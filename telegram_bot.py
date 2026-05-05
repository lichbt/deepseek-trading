"""
Telegram Bot: Notifications, status queries, and alerts for the trading pipeline.
Uses raw Telegram Bot API (no extra dependencies beyond requests).

Setup:
    1. Create bot via @BotFather on Telegram → get token
    2. Start a chat with your bot → get chat_id
    3. Set env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Usage:
    # Run the polling bot in background:
    python telegram_bot.py

    # Or use as notification client from other modules:
    from telegram_bot import notify
    notify("Auto research completed: 2 passed, 8 failed")
"""

import os
import json
import time
import threading
import tempfile
import requests
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime

import pipeline_utils as pu

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
TELEGRAM_API = f'https://api.telegram.org/bot'
OPENROUTER_BASE = 'https://openrouter.ai/api/v1'

_enabled = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

_RESEARCHER_PROMPT = None
_RESEARCHER_PATH = Path(__file__).parent / '.opencode' / 'agents' / 'researcher.md'

def _load_researcher_prompt() -> str:
    global _RESEARCHER_PROMPT
    if _RESEARCHER_PROMPT is None:
        if _RESEARCHER_PATH.exists():
            _RESEARCHER_PROMPT = _RESEARCHER_PATH.read_text()
        else:
            _RESEARCHER_PROMPT = 'You are a quantitative trading strategy researcher. Output only valid JSON.'
    return _RESEARCHER_PROMPT


# ============================================================================
# NOTIFICATION CLIENT (fire-and-forget, used from any module)
# ============================================================================

def _send_raw(text: str, parse_mode: str = 'HTML') -> bool:
    """Send a message to the configured chat. Returns True on success."""
    if not _enabled:
        return False
    try:
        resp = requests.post(
            f'{TELEGRAM_API}{TELEGRAM_TOKEN}/sendMessage',
            json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def notify(message: str) -> bool:
    """Send a plain text notification."""
    return _send_raw(message)


def notify_html(html: str) -> bool:
    """Send an HTML-formatted notification."""
    return _send_raw(html, parse_mode='HTML')


def notify_research_start(instrument: str, target: int, max_iter: int) -> bool:
    return notify_html(
        f'<b>🔬 Auto Research Started</b>\n'
        f'Instrument: {instrument}\n'
        f'Target: {target} passed\n'
        f'Max iterations: {max_iter}\n'
        f'Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}'
    )


def notify_iteration(
    iteration: int,
    strategy_id: str,
    rationale: str,
    is_score: float,
    wf_score: Optional[float],
    ho_score: Optional[float],
    passed: bool
) -> bool:
    emoji = '✅ <b>PASS</b>' if passed else '❌ FAIL'
    wf_text = f'{wf_score:.4f}' if wf_score is not None else 'N/A'
    ho_text = f'{ho_score:.4f}' if ho_score is not None else 'N/A'
    lines = [
        f'{emoji} <b>[{iteration}] {strategy_id}</b>',
        f'<i>{rationale}</i>',
        f'IS: {is_score:.4f}  WF: {wf_text}  HO: {ho_text}',
    ]
    if passed:
        lines.append('🎯 Ready for paper trading!')
    return notify_html('\n'.join(lines))


def notify_research_complete(
    iterations: int,
    passed: List[str],
    failed: int,
    errors: int,
    duration: float,
) -> bool:
    emoji = '🎉' if passed else '😐'
    lines = [
        f'{emoji} <b>Auto Research Complete</b>',
        f'Iterations: {iterations}',
        f'Passed: {len(passed)}  Failed: {failed}  Errors: {errors}',
        f'Duration: {duration:.0f}s',
    ]
    if passed:
        lines.append(f'\nPassed strategy IDs:')
        for pid in passed:
            lines.append(f'  ✅ {pid}')
    return notify_html('\n'.join(lines))


def notify_drawdown_alert(
    strategy_id: str,
    current_drawdown: float,
    action: str,
) -> bool:
    return notify_html(
        f'<b>⚠️ Drawdown Alert</b>\n'
        f'Strategy: {strategy_id}\n'
        f'Drawdown: {current_drawdown:.2%}\n'
        f'Action: <b>{action.upper()}</b>'
    )


def notify_live_metrics(
    strategy_id: str,
    equity: float,
    gt_score: float,
    position: int,
) -> bool:
    pos_str = {1: '📈 LONG', -1: '📉 SHORT', 0: '➖ FLAT'}.get(position, str(position))
    return notify_html(
        f'<b>📊 Live Metrics</b>\n'
        f'Strategy: {strategy_id}\n'
        f'Equity: {equity:,.2f}\n'
        f'GT-Score: {gt_score:.4f}\n'
        f'Position: {pos_str}'
    )


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def _cmd_status() -> str:
    """Build /status response."""
    pu.init_db()
    all_s = pu.get_all_strategies()
    if not all_s:
        return 'No strategies in database yet.'

    statuses = {}
    for s in all_s:
        st = s['status']
        statuses[st] = statuses.get(st, 0) + 1

    lines = ['<b>📊 Pipeline Status</b>', '']
    order = ['proposed', 'research_failed', 'walk_forward_failed',
             'holdout_failed', 'passed', 'paper_trading', 'live', 'retired']
    for st in order:
        if st in statuses:
            emoji = '🟢' if st in ('passed', 'paper_trading', 'live') else '🔴'
            lines.append(f'{emoji} {st}: {statuses[st]}')
    return '\n'.join(lines)


def _cmd_passed() -> str:
    """Build /passed response."""
    pu.init_db()
    passed = pu.get_passed_strategies()
    if not passed:
        return 'No strategies have passed validation yet.'

    lines = ['<b>✅ Passed Strategies</b>', '']
    for s in passed:
        lines.append(f'<b>{s["id"]}</b>')
        if s.get('best_params'):
            params = s['best_params']
            lines.append(f'  Params: {json.dumps(params)}')
    return '\n'.join(lines)


def _cmd_failed() -> str:
    """Build /failed response."""
    pu.init_db()
    failed = pu.get_failed_strategies()
    if not failed:
        return 'No failed strategies yet (or DB is empty).'

    lines = [f'<b>❌ Failed Strategies ({len(failed)})</b>', '']
    for s in failed[:10]:
        status = s.get('final_status', s.get('status', '?'))
        is_s = s.get('is_gt_score')
        wf_s = s.get('wf_gt_score')
        scores = ''
        if is_s is not None:
            scores += f'IS={is_s:.2f}'
        if wf_s is not None:
            scores += f' WF={wf_s:.2f}'
        lines.append(f'<b>{s["id"]}</b> — {status} [{scores}]')
    if len(failed) > 10:
        lines.append(f'\n... and {len(failed) - 10} more')
    return '\n'.join(lines)


def _cmd_research(user_message: str = '') -> str:
    """Run research from natural language prompt.

    Usage: /research <free-text description>
    Examples:
        /research Momentum strategy for EUR_USD using MA crossover
        /research Mean reversion on XAU_USD with RSI
        /research random strategy
    """
    import auto_research
    pu.init_db()
    failed = pu.get_failed_strategies()

    normalized = user_message.strip()
    if not normalized:
        normalized = 'Generate a random trading strategy with a fresh economic hypothesis.'

    random_requests = {
        'random', 'random strategy', 'any strategy', 'surprise me',
        'research random strategies', 'generate random strategy'
    }
    if normalized.lower() in random_requests:
        normalized = 'Generate a random trading strategy with a fresh economic hypothesis.'

    return _research_natural(normalized, failed)


def _research_natural(user_message: str, failed: list) -> str:
    """Run one research cycle from natural language prompt using researcher agent."""
    import auto_research

    system_prompt = _load_researcher_prompt()
    # Prefix user's natural language request to the auto-research prompt
    user_prompt = (
        f"User request: {user_message}\n"
        "If the request is broad or random, choose a fresh, economically grounded strategy archetype yourself.\n\n"
        f"{auto_research._build_user_prompt('EUR_USD', failed, 1)}"
    )

    result = auto_research.call_openrouter(system_prompt, user_prompt)
    if not result['success']:
        return f'❌ LLM error: {result["error"]}'

    candidate = result['candidate']
    required = ['strategy_id', 'code', 'param_grid', 'rationale']
    missing = [k for k in required if k not in candidate]
    if missing:
        return f'❌ LLM returned incomplete JSON (missing {missing})'

    code_err = auto_research._validate_code(candidate['code'])
    if code_err:
        return f'❌ Code rejected: {code_err}'

    fp = pu.compute_strategy_fingerprint(candidate['code'], candidate['param_grid'])
    existing = pu.check_idea_is_new(fp)
    if not existing['new']:
        return f'❌ Duplicate ({existing["status"]})'

    # Resolve instrument — from prompt, or auto-cycle through pool
    inst = _resolve_instrument(candidate.get('instrument'), user_message)
    candidate['instrument'] = inst

    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump(candidate, f, indent=2)
        json_path = f.name

    try:
        passed, message = auto_research.validate_strategy(candidate)
    finally:
        os.unlink(json_path)

    is_score = db_scores.get('is_score')
    wf_score = db_scores.get('wf_score')
    ho_score = db_scores.get('ho_score')
    is_text = f'{is_score:.4f}' if is_score is not None else 'N/A'
    wf_text = f'{wf_score:.4f}' if wf_score is not None else 'N/A'
    ho_text = f'{ho_score:.4f}' if ho_score is not None else 'N/A'

    lines = [
        f'<b>🔬 Research Result</b>',
        f'Strategy: <b>{candidate["strategy_id"]}</b>',
        f'Instrument: {inst}',
        f'<i>{candidate.get("rationale", "none")}</i>',
        f'IS={is_text}  WF={wf_text}  HO={ho_text}',
    ]
    if passed:
        lines.append('✅ <b>PASS</b> — Ready for paper trading!')
    else:
        lines.append(f'❌ {message}')

    return '\n'.join(lines)


# ------------------------------------------------------------------
# Known Oanda-compatible instrument symbols (uppercase, underscore)
# Used for validation and rotation
KNOWN_INSTRUMENTS = [
    'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF', 'USD_CAD', 'AUD_USD',
    'NZD_USD', 'EUR_GBP', 'EUR_JPY', 'GBP_JPY',
    'XAU_USD', 'XAG_USD', 'BCO_USD', 'WTICO_USD',
    'NATGAS_USD', 'CORN_USD', 'SOYBN_USD', 'WHEAT_USD',
    'SPX500_USD', 'US30_USD', 'US100_USD', 'US500_USD',
    'BTC_USD', 'ETH_USD', 'LTC_USD',
]
# Subset used for cycling when no symbol provided (rotate across all for market diversity)
DEFAULT_INSTRUMENT_POOL = [
    'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF',
    'AUD_USD', 'NZD_USD', 'EUR_GBP', 'EUR_JPY', 'GBP_JPY',
    'XAU_USD', 'XAG_USD', 'BCO_USD', 'WTICO_USD',
    'NATGAS_USD', 'CORN_USD', 'SOYBN_USD', 'WHEAT_USD',
    'BTC_USD', 'ETH_USD', 'LTC_USD',
]

# Index for instrument cycling
_instrument_pool_idx = 0

# Instrument extraction patterns
_INSTRUMENT_RE = __import__('re').compile(
    r'\b([A-Z]{3,6}_[A-Z]{3})\b'
)


def _resolve_instrument(instrument_from_llm: Optional[str], user_message: str) -> str:
    """Determine a trading instrument from user's natural language.

    Priority:
    1. Parse valid symbol from user_message (e.g., "EUR_USD", "XAU_USD")
    2. Fall back to cycling default pool for diversity
    3. Default to EUR_USD as final fallback

    This allows natural language without requiring explicit symbols.
    """
    global _instrument_pool_idx

    # 1. Try to extract from user message
    found = _INSTRUMENT_RE.findall(user_message.upper())
    for inst in found:
        if inst in KNOWN_INSTRUMENTS:
            return inst

    # 2. Cycle through default pool for diversity (no symbol provided)
    inst = DEFAULT_INSTRUMENT_POOL[_instrument_pool_idx % len(DEFAULT_INSTRUMENT_POOL)]
    _instrument_pool_idx += 1

    return inst  # 'EUR_USD', then 'XAU_USD', then 'GBP_USD', etc.


def _cmd_help() -> str:
    return (
        '<b>🤖 Trading Pipeline Bot</b>\n\n'
        '/status — Pipeline overview\n'
        '/passed — Strategies that passed validation\n'
        '/failed — Failed strategies with scores\n'
        '/autorun — Run auto research (30 iter, target 1)\n'
        '/research <prompt> — Generate & validate from natural language\n'
        '/research Mean reversion with ATR bands\n'
        '/research Momentum strategy for EUR_USD using MA crossover\n'
        '/help — This message\n\n'
        'Or just chat — any message is forwarded to AI.'
    )


# Background thread state
_autorun_thread = None
_autorun_status = {'running': False, 'message_id': None}


def _run_autorun():
    """Run auto research in background thread, send summary to Telegram when done."""
    global _autorun_status
    import auto_research

    pu.init_db()
    ar = auto_research.AutoResearcher(instruments=['EUR_USD'])
    results = ar.run(target_passed=1, max_iterations=30)

    passed = len(results.get('passed', []))
    failed = len(results.get('failed', []))
    errors = results.get('errors', 0)
    duration = results.get('duration_seconds', 0)

    emoji = '🎉' if passed else '😐'
    lines = [
        f'{emoji} <b>Auto Research Complete</b>',
        f'Iterations: {results.get("iterations", 0)}',
        f'Passed: {passed}  Failed: {failed}  Errors: {errors}',
        f'Duration: {duration:.0f}s',
    ]
    if passed:
        lines.append(f'\nPassed:')
        for pid in results.get('passed', []):
            lines.append(f'  ✅ {pid}')
    else:
        lines.append('\nNo strategies passed. Check program.md for updated research directives.')

    notify_html('\n'.join(lines))
    _autorun_status['running'] = False


def _get_scores(strategy_id: str) -> Dict[str, Optional[float]]:
    """Get validation scores from DB."""
    with pu.get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT is_gt_score, walk_forward_gt_score, holdout_gt_score FROM validation_results WHERE strategy_id = ?',
            (strategy_id,)
        )
        row = c.fetchone()
        if row:
            return {
                'is_score': row['is_gt_score'],  # May be None if not evaluated
                'wf_score': row['walk_forward_gt_score'],
                'ho_score': row['holdout_gt_score'],
            }
    return {'is_score': None, 'wf_score': None, 'ho_score': None}


def _cmd_autorun() -> str:
    """Start auto research in background thread."""
    global _autorun_thread

    if _autorun_status['running']:
        return '⚠️ Auto research already running. Wait for it to finish.'

    notify_html('🔬 <b>Auto Research Started</b>\n'
                'Running 30 iterations, target 1 pass.\n'
                'You\'ll get a summary when done.')

    import threading
    _autorun_thread = threading.Thread(target=_run_autorun, daemon=True)
    _autorun_thread.start()
    _autorun_status['running'] = True

    return '🚀 Auto research launched! I\'ll notify you when it\'s done.'


COMMANDS = {
    '/start': _cmd_help,
    '/help': _cmd_help,
    '/status': _cmd_status,
    '/passed': _cmd_passed,
    '/failed': _cmd_failed,
    '/research': _cmd_research,
    '/autorun': _cmd_autorun,
}


def _chat_with_ai(user_message: str) -> str:
    """Forward user message to OpenRouter DeepSeek and return reply."""
    if not OPENROUTER_API_KEY:
        return "OpenRouter API key not configured."
    try:
        resp = requests.post(
            f'{OPENROUTER_BASE}/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-chat'),
                'messages': [
                    {'role': 'system', 'content': 'You are a trading strategy assistant. Answer concisely in 2-5 lines.'},
                    {'role': 'user', 'content': user_message},
                ],
                'temperature': 0.7,
                'max_tokens': 800,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        return f'Error: {e}'


def handle_update(update: Dict) -> Optional[str]:
    """Process a Telegram update, return response text or None."""
    if 'message' not in update:
        return None
    msg = update['message']
    text = msg.get('text', '')
    chat_id = str(msg.get('chat', {}).get('id', ''))

    if not text:
        return None

    if text.startswith('/'):
        parts = text.split()
        cmd = parts[0].split('@')[0]
        args = parts[1:] if len(parts) > 1 else []
        handler = COMMANDS.get(cmd)
        if handler:
            if cmd == '/research' and args:
                # Pass full natural-language prompt as single string
                return handler(' '.join(args))
            if args:
                return handler(args[0])
            return handler()
        return None
    
    return _chat_with_ai(text)


# ============================================================================
# POLLING BOT
# ============================================================================

class TelegramBot:
    """Long-polling bot that listens for commands."""

    def __init__(self, token: str = None):
        self.token = token or TELEGRAM_TOKEN
        if not self.token:
            raise ValueError('TELEGRAM_BOT_TOKEN not set')
        self.base_url = f'{TELEGRAM_API}{self.token}'
        self._running = False
        self._offset = 0

    def _get_updates(self, timeout: int = 30) -> List[Dict]:
        try:
            resp = requests.get(
                f'{self.base_url}/getUpdates',
                params={'offset': self._offset, 'timeout': timeout},
                timeout=timeout + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('result', [])
        except Exception:
            return []

    def run(self):
        """Start polling loop. Runs until interrupted."""
        pu.init_db()
        self._running = True
        print(f'🤖 Telegram bot started. Commands: /status /passed /failed /help')

        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update['update_id'] + 1

                    resp_text = handle_update(update)
                    if resp_text:
                        chat_id = update.get('message', {}).get('chat', {}).get('id')
                        if chat_id:
                            requests.post(
                                f'{self.base_url}/sendMessage',
                                json={
                                    'chat_id': chat_id,
                                    'text': resp_text,
                                    'parse_mode': 'HTML',
                                },
                                timeout=10,
                            )
            except KeyboardInterrupt:
                self._running = False
            except Exception as e:
                print(f'Bot error: {e}')
                time.sleep(5)

        print('Bot stopped.')

    def stop(self):
        self._running = False


# ============================================================================
# CLI
# ============================================================================

def main():
    if not TELEGRAM_TOKEN:
        print('ERROR: TELEGRAM_BOT_TOKEN not set.')
        print('Export: TELEGRAM_BOT_TOKEN=your_token')
        return

    bot = TelegramBot()
    bot.run()


if __name__ == '__main__':
    main()
