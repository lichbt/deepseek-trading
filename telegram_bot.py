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
import re
import json
import time
import threading
import tempfile
import requests
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime

import pipeline_utils as pu

TELEGRAM_TOKEN = os.getenv('TOMI_TELEGRAM_BOT_TOKEN', os.getenv('TELEGRAM_BOT_TOKEN', ''))
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


def _send_with_buttons(text: str, buttons: list) -> bool:
    """Send a message with inline keyboard buttons.
    buttons: list of list of {text, callback_data}
    """
    if not _enabled:
        return False
    try:
        resp = requests.post(
            f'{TELEGRAM_API}{TELEGRAM_TOKEN}/sendMessage',
            json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
                'reply_markup': {'inline_keyboard': buttons},
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def notify_strategy_passed(
    strategy_id: str,
    instrument: str,
    timeframe: str,
    rationale: str,
    is_score: float,
    wf_score: float,
    best_params: dict,
) -> bool:
    """Send pass notification with Deploy / Skip inline buttons."""
    params_str = json.dumps(best_params)[:120]
    text = (
        f'🎯 <b>Strategy Passed — Deploy?</b>\n\n'
        f'<b>{strategy_id}</b>\n'
        f'Instrument: {instrument}  TF: {timeframe}\n'
        f'<i>{rationale}</i>\n\n'
        f'IS: {is_score:.4f}  WF: {wf_score:.4f}\n'
        f'Params: {params_str}'
    )
    buttons = [[
        {'text': '✅ Deploy', 'callback_data': f'deploy:{strategy_id}'},
        {'text': '❌ Skip',   'callback_data': f'skip:{strategy_id}'},
    ]]
    return _send_with_buttons(text, buttons)


def _infer_instrument(strategy_id: str) -> str:
    """Infer OANDA instrument from strategy_id prefix."""
    _PREFIX_MAP = {
        'EUR_USD': 'EUR_USD', 'GBP_USD': 'GBP_USD', 'USD_JPY': 'USD_JPY',
        'USD_CHF': 'USD_CHF', 'AUD_USD': 'AUD_USD', 'NZD_USD': 'NZD_USD',
        'GBP_JPY': 'GBP_JPY', 'EUR_JPY': 'EUR_JPY', 'EUR_GBP': 'EUR_GBP',
        'XAU_USD': 'XAU_USD', 'XAG_USD': 'XAG_USD', 'BCO_USD': 'BCO_USD',
        'BTC_USD': 'BTC_USD', 'ETH_USD': 'ETH_USD', 'WTICO_USD': 'WTICO_USD',
        'NATGAS_USD': 'NATGAS_USD', 'CORN_USD': 'CORN_USD',
        'SOYBN_USD': 'SOYBN_USD', 'WHEAT_USD': 'WHEAT_USD', 'LTC_USD': 'LTC_USD',
    }
    _INSTRUMENT_MAP = {
        'EURUSD': 'EUR_USD', 'GBPUSD': 'GBP_USD', 'USDJPY': 'USD_JPY',
        'USDCHF': 'USD_CHF', 'AUDUSD': 'AUD_USD', 'NZDUSD': 'NZD_USD',
        'GBPJPY': 'GBP_JPY', 'EURJPY': 'EUR_JPY', 'EURGBP': 'EUR_GBP',
        'XAUUSD': 'XAU_USD', 'XAGUSD': 'XAG_USD', 'BCOUSD': 'BCO_USD',
        'WTICOUSD': 'WTICO_USD', 'NATGASUSD': 'NATGAS_USD',
        'BTCUSD': 'BTC_USD', 'ETHUSD': 'ETH_USD', 'LTCUSD': 'LTC_USD',
        'CORNUSD': 'CORN_USD', 'SOYBNUSD': 'SOYBN_USD', 'WHEATUSD': 'WHEAT_USD',
    }
    sid_upper = strategy_id.upper()
    for prefix, inst in _PREFIX_MAP.items():
        p = prefix.replace('_', '') + '_'
        if sid_upper.startswith(prefix.upper() + '_') or sid_upper.startswith(p.upper()):
            return inst
    raw = strategy_id.split('_auto_')[0].upper().replace('_', '')
    return _INSTRUMENT_MAP.get(raw, 'EUR_USD')


def _deploy_strategy(strategy_id: str) -> str:
    """Deploy a passed strategy: mark paper_trading + spawn trader + rebalance portfolio."""
    import subprocess

    project_dir = Path(__file__).parent

    # 1. Cap check — max 15 live strategies
    with pu.get_db_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE status='paper_trading'"
        ).fetchone()[0]
    if count >= 15:
        return (f'⚠️ Cap reached: {count}/15 strategies already live.\n'
                f'Retire one before deploying {strategy_id}.')

    # 2. Check strategy is actually in passed status
    with pu.get_db_connection() as conn:
        row = conn.execute(
            'SELECT status FROM strategies WHERE id = ?', (strategy_id,)
        ).fetchone()
    if not row:
        return f'❌ Strategy {strategy_id} not found in DB.'
    if row[0] not in ('passed', 'passed_but_fragile'):
        return f'❌ Status is "{row[0]}", not passed — cannot deploy.'

    # 3. Mark as paper_trading in DB
    try:
        pu.start_live_trading(strategy_id)
    except Exception as e:
        return f'❌ DB update failed: {e}'

    # 4. Spawn live_test.py process
    instrument = _infer_instrument(strategy_id)
    python = str(project_dir / 'venv' / 'bin' / 'python')
    log_dir = project_dir / '.paper-trading-logs'
    log_dir.mkdir(exist_ok=True)
    log_file = str(log_dir / f'{strategy_id}.log')
    env = os.environ.copy()

    try:
        subprocess.Popen(
            ['caffeinate', '-i', python, '-u',
             str(project_dir / 'live_test.py'), strategy_id, '--instrument', instrument],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        print(f'[Deploy] Spawned live_test.py for {strategy_id} ({instrument})', flush=True)
    except Exception as e:
        return f'⚠️ DB updated but failed to spawn trader: {e}'

    # 5. Rebalance portfolio weights
    try:
        subprocess.run(
            [python, str(project_dir / 'portfolio.py'), '--write'],
            cwd=str(project_dir), timeout=60, capture_output=True, env=env,
        )
        print(f'[Deploy] Portfolio rebalanced.', flush=True)
    except Exception as e:
        print(f'[Deploy] Portfolio rebalance failed (non-fatal): {e}', flush=True)

    return f'✅ Deployed <b>{strategy_id}</b> on {instrument}.\nPortfolio rebalanced.'


def _handle_callback_query(callback_query: dict) -> None:
    """Handle inline button presses (deploy/skip)."""
    query_id = callback_query.get('id')
    data = callback_query.get('data', '')
    chat_id = callback_query.get('message', {}).get('chat', {}).get('id')
    message_id = callback_query.get('message', {}).get('message_id')

    # Acknowledge the button press immediately
    try:
        requests.post(
            f'{TELEGRAM_API}{TELEGRAM_TOKEN}/answerCallbackQuery',
            json={'callback_query_id': query_id},
            timeout=5,
        )
    except Exception:
        pass

    if not data or ':' not in data:
        return

    action, strategy_id = data.split(':', 1)

    if action == 'deploy':
        response = _deploy_strategy(strategy_id)
    elif action == 'skip':
        response = f'⏭️ Skipped <b>{strategy_id}</b> — stays in passed status.'
    else:
        return

    # Edit the original message to remove buttons and show result
    try:
        requests.post(
            f'{TELEGRAM_API}{TELEGRAM_TOKEN}/editMessageReplyMarkup',
            json={'chat_id': chat_id, 'message_id': message_id, 'reply_markup': {'inline_keyboard': []}},
            timeout=5,
        )
        requests.post(
            f'{TELEGRAM_API}{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': response, 'parse_mode': 'HTML'},
            timeout=5,
        )
    except Exception:
        pass


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

    code_err, cleaned_code = auto_research._validate_code(candidate['code'])
    if code_err:
        return f'❌ Code rejected: {code_err}'
    candidate['code'] = cleaned_code

    # Resolve instrument — from prompt, or auto-cycle through pool
    inst = _resolve_instrument(candidate.get('instrument'), user_message)
    candidate['instrument'] = inst

    fp = pu.compute_strategy_fingerprint(candidate['code'], candidate['param_grid'], candidate.get('timeframe', 'D'), inst)
    existing = pu.check_idea_is_new(fp)
    if not existing['new']:
        return f'❌ Duplicate ({existing["status"]})'

    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump(candidate, f, indent=2)
        json_path = f.name

    try:
        passed, message = auto_research.validate_strategy(candidate)
    finally:
        os.unlink(json_path)

    db_scores = _get_scores(candidate['strategy_id'])
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
_INSTRUMENT_RE = re.compile(r'\b([A-Z]{3,6}_[A-Z]{3})\b')


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
        '/compare <id1> <id2> [id3] — Side-by-side comparison\n'
        '/autorun — Run auto research (30 iter, target 1, historical spreads)\n'
        '/research <prompt> — Generate & validate from natural language\n'
        '/research Mean reversion with ATR bands\n'
        '/research Momentum strategy for EUR_USD using MA crossover\n'
        '/help — This message\n\n'
        'Or just chat — any message is forwarded to AI.'
    )


def _cmd_compare(args: List[str]) -> str:
    """Build /compare response for 2-3 strategies."""
    if not args:
        return 'Usage: /compare <id1> <id2> [id3]'

    strategy_ids = args[:3]
    if len(strategy_ids) < 2:
        return 'Please provide at least 2 strategy IDs to compare.'

    lines = ['<b>📊 Strategy Comparison</b>', '']

    from pipeline_utils import get_strategy_by_id
    from pipeline_utils import get_db_connection

    for sid in strategy_ids:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT s.id, s.status, s.rationale, s.timeframe, s.param_grid,
                       vr.is_gt_score, vr.walk_forward_gt_score, vr.holdout_gt_score, vr.best_params
                FROM strategies s
                LEFT JOIN validation_results vr ON s.id = vr.strategy_id
                WHERE s.id = ?
            ''', (sid,))
            row = c.fetchone()

        if not row:
            lines.append(f'⚠️ {sid} — not found in database')
            continue

        sid, status, rationale, timeframe, param_grid, is_score, wf_score, ho_score, best_params = row

        is_text = f'{is_score:.4f}' if is_score is not None else 'N/A'
        wf_text = f'{wf_score:.4f}' if wf_score is not None else 'N/A'
        ho_text = f'{ho_score:.4f}' if ho_score is not None else 'N/A'
        emoji = '✅' if status == 'passed' else ('📊' if status == 'paper_trading' else '❌') if 'failed' in status else '⏳'

        lines.append(f'{emoji} <b>{sid}</b> [{status}]')
        lines.append(f'  Timeframe: {timeframe}')
        lines.append(f'  IS={is_text} WF={wf_text} HO={ho_text}')
        if best_params:
            import json
            lines.append(f'  Params: {json.dumps(json.loads(best_params))[:60]}...')
        lines.append('')

    return '\n'.join(lines)


# Background thread state
_autorun_thread = None
_autorun_status = {'running': False, 'message_id': None}


def _run_autorun():
    """Run auto research in background thread, send summary to Telegram when done."""
    global _autorun_status
    import os
    os.environ['USE_HISTORICAL_SPREADS'] = '1'  # Force realistic spread modeling
    import auto_research

    pu.init_db()
    ar = auto_research.AutoResearcher()  # cycles full pool, no single instrument
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


# Forward-declare for COMMANDS dict before function is defined
def _cmd_autorun_status() -> str:
    """Build /autorun_status response."""
    import subprocess, datetime

    lines = ['<b>🔬 Auto-Research Status</b>', '']

    # Check Telegram-side background thread
    if _autorun_status['running']:
        lines.append('🟡 Telegram /autorun: RUNNING in background thread')
    else:
        lines.append('⚪ Telegram /autorun: idle')

    # Check external auto_research.py process
    try:
        result = subprocess.run(
            ['ps', '-ax', '-o', 'pid,etime,command'],
            capture_output=True, text=True, timeout=5
        )
        external_lines = []
        for line in result.stdout.splitlines():
            if 'auto_research.py' in line and 'grep' not in line:
                parts = line.strip().split(None, 2)
                if len(parts) >= 3:
                    pid, elapsed, cmd = parts[0], parts[1], parts[2]
                    if 'python' in cmd:
                        cmd_short = cmd.split('/')[-1] if '/' in cmd else cmd
                        external_lines.append(f'🟢 PID {pid} | elapsed {elapsed} | {cmd_short}')
        if external_lines:
            lines.append('')
            lines.append('<b>External auto_research.py processes:</b>')
            lines.extend(external_lines)
        else:
            lines.append('')
            lines.append('⚪ No external auto_research.py process running')
    except Exception as e:
        lines.append(f'⚠️ Could not check processes: {e}')

    # Recent strategies from DB
    pu.init_db()
    with pu.get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT id, status, created_at FROM strategies
            ORDER BY created_at DESC LIMIT 5
        ''')
        rows = c.fetchall()

    if rows:
        lines.append('')
        lines.append('<b>Recent strategies:</b>')
        for row in rows:
            sid, status, created = row
            age = ''
            try:
                ts = datetime.datetime.fromisoformat(created.replace('Z', '+00:00'))
                delta = datetime.datetime.now(datetime.timezone.utc) - ts
                mins = int(delta.total_seconds() // 60)
                age = f'{mins}m ago'
            except Exception:
                pass
            emoji = '✅' if status == 'passed' else ('📊' if status == 'paper_trading' else '❌' if 'failed' in status else '⏳')
            lines.append(f'{emoji} {sid} [{status}] {age}')

    return '\n'.join(lines)


COMMANDS = {
    '/start': _cmd_help,
    '/help': _cmd_help,
    '/status': _cmd_status,
    '/passed': _cmd_passed,
    '/failed': _cmd_failed,
    '/compare': _cmd_compare,
    '/research': _cmd_research,
    '/autorun': _cmd_autorun,
    '/autorun_status': _cmd_autorun_status,
}


def _chat_with_ai(user_message: str) -> str:
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
    # Handle inline button presses
    if 'callback_query' in update:
        _handle_callback_query(update['callback_query'])
        return None

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
                return handler(' '.join(args))
            if cmd == '/compare':
                return handler(args)
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
