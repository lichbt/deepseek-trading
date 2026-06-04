#!/usr/bin/env python3
"""
Periodic status report → Telegram (every 4h).

Two sections:
  1. Auto-research activity in the last 4h (validated candidates, pass/fail
     breakdown, best scores, staleness check).
  2. Live paper-trading status (each deployed strategy: equity, P&L, GT-score,
     position, last-update staleness).

Run manually:   python hourly_report.py
Scheduled:      launchd com.lich.hourlyreport (StartInterval 14400)
"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

from telegram_bot import notify_html

DB_PATH = Path(__file__).parent / 'pipeline.db'
WINDOW_MIN = 240         # lookback window for the report (matches the 4h cadence)
WINDOW_LABEL = '4h'      # human label for the window
STALL_MIN = 45           # warn if no auto-research validation in this many minutes


def _parse_dt(s: str):
    """Parse an ISO timestamp to an aware UTC datetime (best-effort)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_str(dt) -> str:
    if dt is None:
        return 'n/a'
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f'{int(secs // 60)}m ago'
    if secs < 86400:
        return f'{secs / 3600:.1f}h ago'
    return f'{secs / 86400:.1f}d ago'


def _instrument_from_id(sid: str) -> str:
    """Best-effort instrument label from a strategy id like 'nzdusd_auto_...'."""
    base = sid.split('_auto_')[0] if '_auto_' in sid else sid.split('_')[0]
    base = base.upper()
    # Codes are <symbol><3-char quote>, e.g. EURUSD, EURJPY, WHEATUSD, NATGASUSD.
    # Split off the trailing 3-char quote currency.
    if len(base) >= 6:
        return f'{base[:-3]}_{base[-3:]}'
    return base


def _stage_of(status: str) -> str:
    s = (status or '').lower()
    if 'pass' in s:
        return 'pass'
    if s.startswith('fail: is') or ' is ' in s:
        return 'IS'
    if 'wf' in s or 'walk' in s:
        return 'WF'
    if 'sparse' in s:
        return 'sparse'
    if 'holdout' in s or 'decay' in s:
        return 'holdout'
    if 'regime' in s or 'single-regime' in s:
        return 'regime'
    if 'directional' in s:
        return 'bias'
    if 'duplicate' in s:
        return 'dup'
    if 'timed out' in s or 'timeout' in s:
        return 'timeout'
    return 'other'


def build_research_section(cur) -> str:
    now = datetime.now(timezone.utc)
    rows = cur.execute(
        "SELECT strategy_id, is_gt_score, walk_forward_gt_score, final_status, tested_at "
        "FROM validation_results ORDER BY tested_at DESC LIMIT 200"
    ).fetchall()

    recent = []
    newest_dt = None
    for r in rows:
        dt = _parse_dt(r['tested_at'])
        if dt is None:
            continue
        if newest_dt is None or dt > newest_dt:
            newest_dt = dt
        if (now - dt).total_seconds() <= WINDOW_MIN * 60:
            recent.append((r, dt))

    if not recent:
        stalled = newest_dt is None or (now - newest_dt).total_seconds() > STALL_MIN * 60
        warn = f'\n  ⚠️ <b>STALLED</b> — no validation in over {STALL_MIN}m' if stalled else ''
        return (f'🔬 <b>Auto-Research (last {WINDOW_LABEL})</b>\n'
                f'  No candidates validated.\n'
                f'  Last activity: {_age_str(newest_dt)}{warn}')

    stages = {}
    passes = []
    best_is = best_wf = 0.0
    for r, _dt in recent:
        st = _stage_of(r['final_status'])
        stages[st] = stages.get(st, 0) + 1
        if st == 'pass':
            passes.append(r['strategy_id'])
        best_is = max(best_is, r['is_gt_score'] or 0.0)
        best_wf = max(best_wf, r['walk_forward_gt_score'] or 0.0)

    n = len(recent)
    npass = stages.get('pass', 0)
    nfail = n - npass
    fail_breakdown = ', '.join(
        f'{k} {v}' for k, v in sorted(stages.items(), key=lambda kv: -kv[1]) if k != 'pass'
    ) or '—'

    lines = [
        f'🔬 <b>Auto-Research (last {WINDOW_LABEL})</b>',
        f'  Validated: {n} | ✅ {npass} pass | ❌ {nfail} fail',
        f'  Best: IS={best_is:.3f} WF={best_wf:.3f}',
        f'  Fail stages: {fail_breakdown}',
        f'  Last activity: {_age_str(newest_dt)}',
    ]
    if (now - newest_dt).total_seconds() > STALL_MIN * 60:
        lines.append('  ⚠️ <b>STALLED</b> — last run > {}m ago'.format(STALL_MIN))
    if passes:
        lines.append('  🎉 PASSED: ' + ', '.join(p[:36] for p in passes))
    return '\n'.join(lines)


def build_live_section(cur) -> str:
    # Only currently-trading strategies. live_status rows are preserved after a
    # strategy is retired (to keep its equity history), so an unfiltered query
    # also lists retired/superseded strategies and inflates the live count.
    # Join strategies and keep only status='paper_trading'.
    rows = cur.execute(
        "SELECT ls.strategy_id, ls.equity_curve, ls.current_gt_score, "
        "ls.current_position, ls.last_updated, ls.start_date "
        "FROM live_status ls JOIN strategies s ON s.id = ls.strategy_id "
        "WHERE s.status = 'paper_trading' ORDER BY ls.start_date"
    ).fetchall()
    if not rows:
        return '📈 <b>Live Paper</b>\n  No strategies deployed.'

    pos_map = {1: 'LONG', -1: 'SHORT', 0: 'FLAT'}
    lines = [f'📈 <b>Live Paper ({len(rows)} strategies)</b>']
    for r in rows:
        sid = r['strategy_id']
        inst = _instrument_from_id(sid)
        try:
            curve = json.loads(r['equity_curve']) if r['equity_curve'] else []
        except Exception:
            curve = []
        if curve:
            equity = curve[-1].get('equity', 0.0)
            start_eq = curve[0].get('equity', equity)
            pnl_pct = ((equity / start_eq) - 1.0) * 100 if start_eq else 0.0
            eq_str = f'${equity:,.0f} ({pnl_pct:+.2f}%)'
        else:
            eq_str = 'n/a'
        gt = r['current_gt_score'] or 0.0
        pos = pos_map.get(r['current_position'], '?')
        age = _age_str(_parse_dt(r['last_updated']))
        lines.append(f'  • {inst} | {eq_str} | GT {gt:.3f} | {pos} | upd {age}')
    return '\n'.join(lines)


def build_report() -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        header = f'📊 <b>Status Report (4h)</b> — {datetime.now().strftime("%H:%M")}'
        research = build_research_section(cur)
        live = build_live_section(cur)
    finally:
        conn.close()
    # Account-level prop-firm drawdown limits (never let a failure break the report)
    try:
        import prop_guard
        prop = prop_guard.report_section()
    except Exception:
        prop = ''
    parts = [header, research, live] + ([prop] if prop else [])
    return '\n\n'.join(parts)


def main():
    report = build_report()
    ok = notify_html(report)
    print(report)
    print('\n[telegram sent]' if ok else '\n[telegram NOT sent — check token/chat env]')


if __name__ == '__main__':
    main()
