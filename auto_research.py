"""
Auto Research: Automated strategy generation + validation loop.
Uses OpenRouter (Gemini Flash) to generate candidates, then runs them
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

# Default OpenRouter model (Qwen Coder - optimized for code)
# Primary: qwen, Fallback: deepseek-chat (if rate limited)
DEFAULT_MODEL = 'qwen/qwen3-coder:free'
FALLBACK_MODEL = 'deepseek/deepseek-chat'

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
    print(f'  Prompt size: ~{estimated_prompt_tokens} tokens')

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
        content = data['choices'][0]['message']['content'] or ''

        candidate = _extract_json(content)
        if candidate is None:
            return {'success': False, 'candidate': None, 'error': f'Failed to parse JSON: {content[:200]}'}

        return {'success': True, 'candidate': candidate, 'error': None}

    except requests.exceptions.Timeout:
        return {'success': False, 'candidate': None, 'error': 'OpenRouter timeout'}
    except requests.exceptions.RequestException as e:
        return {'success': False, 'candidate': None, 'error': f'API error: {e}'}
    except Exception as e:
        return {'success': False, 'candidate': None, 'error': f'Unexpected error: {e}'}


def _validate_code(code: str) -> Optional[str]:
    """Validate strategy code before execution. Returns error string or None."""
    if not code or 'generate_signals' not in code:
        return 'missing generate_signals function'
    if 'df["volume"]' in code or "df['volume']" in code or 'df.volume' in code:
        return 'references df volume column (does not exist in OHLC data)'
    if "'Volume'" in code or '"Volume"' in code:
        return 'references Volume column'
    if 'shift(-1)' in code:
        return 'uses look-ahead bias (shift(-1))'
    if 'import talib' in code:
        return 'uses talib instead of ta library'
    if 'import pandas' not in code and 'import pd' not in code:
        return 'missing import pandas / import pd'
    has_ta = 'import ta' in code or 'from ta' in code
    has_np = 'import numpy' in code or 'import np' in code
    if not has_ta and not has_np:
        return 'missing import ta or import numpy (need at least one)'
    if not ('df.low' in code or 'df.high' in code or 'df["close"]' in code or "df['close']" in code or 'df[\'close\']' in code):
        return 'never references price data (close/high/low)'

    # Check for WRONG ta API calls (common LLM mistakes)
    # CCI is in ta.trend, NOT ta.momentum
    if 'ta.momentum.cci' in code:
        return 'use ta.trend.cci NOT ta.momentum.cci'
    # Aroon is ta.trend.aroon_up/aroon_down, NOT ta.trend.aroon (DataFrame)
    if 'ta.trend.aroon[' in code or 'ta.trend.aroon(' in code:
        return 'use ta.trend.aroon_up() and ta.trend.aroon_down() (returns Series)'
    # Supertrend is in ta.trend, check API
    if 'ta.volatility.supertrend' in code:
        return 'use ta.trend.supertrendindicator from ta.trend'
    # Williams %R is in ta.momentum, correct
    if 'ta.trend.williams' in code:
        return 'use ta.momentum.williams_r'

    return None


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
        os.environ.setdefault('OANDA_ACCOUNT_ID', os.environ.get('OANDA_ACCOUNT_ID', '101-011-13677064-003'))
        os.environ.setdefault('OANDA_API_TOKEN', os.environ.get('OANDA_API_TOKEN', '43f5e160ff289434d6248e5414cc226f-66bdf18f9199213b719671a19ac96998'))
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
        if len(df) < 30:
            return None

        signals = fn(df, first_params)
        non_zero = int((signals != 0).sum())

        if non_zero < min_signals:
            return f'only {non_zero} signals (min {min_signals} needed)'

        return None
    except Exception:
        return None  # data fetch issue — let validator handle it

    return None


def _extract_json(text: str) -> Optional[Dict]:
    """Try to extract JSON from LLM output (may have markdown fences)."""
    text = text.strip()

    if text.startswith('```'):
        lines = text.split('\n')
        if len(lines) > 2:
            text = '\n'.join(lines[1:-1])
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object between { and }
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
        fp = pu.compute_strategy_fingerprint(code, param_grid)
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

                # Step 2: Build prompts
                system_prompt = _build_system_prompt()
                user_prompt = _build_user_prompt(instrument, failed, iteration)

                # Step 3: Call LLM
                print(f"\n[Iteration {iteration}/{max_iterations}] {instrument}")
                print(f"  Querying {self.model}...")

                llm_result = call_openrouter(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=self.model,
                    api_key=self.api_key,
                    temperature=self.temperature,
                )

                if not llm_result['success']:
                    error_msg = llm_result['error']
                    # Auto-retry with fallback model on 429 (rate limit)
                    if '429' in error_msg or 'Too Many Requests' in error_msg:
                        print(f"  ! Rate limited on primary model, switching to fallback...")
                        time.sleep(2)
                        llm_result = call_openrouter(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            model=FALLBACK_MODEL,
                            api_key=self.api_key,
                            temperature=self.temperature,
                        )
                        if llm_result['success']:
                            print(f"  ✓ Fallback model succeeded")
                            self.model = FALLBACK_MODEL  # keep using fallback for this run
                        else:
                            error_msg = llm_result['error']

                    if not llm_result['success']:
                        print(f"  ✗ LLM error: {error_msg}")
                        results['errors'] += 1
                        time.sleep(self.min_delay)
                        continue

                candidate = llm_result['candidate']

                candidate['strategy_id'] = self._generate_strategy_id(
                    instrument.lower().replace('_', ''), iteration
                )
                tf = candidate.get('timeframe', 'D')
                if tf is None or isinstance(tf, list):
                    tf = 'D'
                candidate['timeframe'] = tf

                # Step 4: Validate candidate structure
                required = ['strategy_id', 'code', 'param_grid', 'rationale', 'timeframe']
                missing = [k for k in required if k not in candidate]
                if missing:
                    print(f"  ✗ Missing keys: {missing}")
                    results['errors'] += 1
                    continue

                candidate['instrument'] = instrument

                # Step 4b: Validate code quality (with simple strategy enforcement)
                code_err = _validate_code(candidate['code'])
                if code_err:
                    # Retry once with feedback
                    print(f"  ! Code issue: {code_err}, retrying...")
                    fix_result = call_openrouter(
                        system_prompt=system_prompt,
                        user_prompt=f"The previous candidate had this error: {code_err}\n\nFix the code and return a corrected candidate JSON.",
                        model=self.model,
                        api_key=self.api_key,
                        temperature=0.3,
                    )
                    if fix_result['success'] and fix_result['candidate']:
                        candidate = fix_result['candidate']
                        code_err = _validate_code(candidate['code'])
                        if code_err:
                            print(f"  ✗ Retry failed: {code_err}")
                            results['errors'] += 1
                            continue
                    else:
                        print(f"  ✗ Retry error: {fix_result.get('error', 'failed')}")
                        results['errors'] += 1
                        continue

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
