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
import os
import time
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

from telegram_bot import notify_html
import html as _html


def _telegram_safe_html(text: str) -> str:
    """Escape stray <, >, & in dynamic content (e.g. '(< 5)', 'IS 0.21 < 0.3',
    'P&L') so Telegram's HTML parser doesn't 400 the WHOLE message, while
    preserving the only tag the report intentionally uses (<b>). Without this a
    single '<' in any section silently drops the entire report."""
    safe = _html.escape(text, quote=False)
    return safe.replace('&lt;b&gt;', '<b>').replace('&lt;/b&gt;', '</b>')

ROOT = Path(__file__).parent
DB_PATH = ROOT / 'pipeline.db'
FIX_STATE_PATH = ROOT / 'fix_runner_state.json'
FIX_LOG_PATH = ROOT / '.fix-logs' / 'fix.log'
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
    try:
        import strategy_honesty as _H
        _tpp = _H.trials_per_ho_pass(str(Path(__file__).parent / 'trials.db'))
        if _tpp:
            lines.append(f'  Trials/HO-pass: {_tpp:.0f}:1 (deflate HO accordingly)')
    except Exception:
        pass
    return '\n'.join(lines)


def build_live_section(cur) -> str:
    # Only currently-trading strategies. live_status rows are preserved after a
    # strategy is retired (to keep its equity history), so an unfiltered query
    # also lists retired/superseded strategies and inflates the live count.
    # Keep paper_trading AND incubating. Incubating sleeves trade the paper book
    # only; they are tagged below so they are never read as live prop exposure.
    rows = cur.execute(
        "SELECT COALESCE(ls.strategy_id, su.sleeve_id) AS strategy_id, "
        "ls.equity_curve, ls.current_gt_score, ls.current_position, "
        "ls.last_updated, ls.start_date, s.status, su.units "
        "FROM live_status ls "
        "LEFT JOIN sleeve_units su ON su.sleeve_id = ls.strategy_id "
        "JOIN strategies s ON s.id = ls.strategy_id "
        "WHERE s.status IN ('paper_trading','incubating') OR COALESCE(su.units, 0) != 0 "
        "UNION ALL "
        "SELECT su.sleeve_id, NULL, NULL, 0, NULL, NULL, s.status, su.units "
        "FROM sleeve_units su JOIN strategies s ON s.id = su.sleeve_id "
        "LEFT JOIN live_status ls ON ls.strategy_id = su.sleeve_id "
        "WHERE ABS(su.units) > 0 AND ls.strategy_id IS NULL "
        "ORDER BY start_date"
    ).fetchall()
    if not rows:
        return '📈 <b>Live Paper</b>\n  No strategies deployed.'

    # NETTING stores per-sleeve ownership separately; live_status can lag after
    # a process restart, while sleeve_units is the persisted broker-share truth.
    try:
        unit_rows = cur.execute('SELECT sleeve_id, units FROM sleeve_units').fetchall()
        positions = {r['sleeve_id']: (1 if r['units'] > 0 else -1 if r['units'] < 0 else 0)
                     for r in unit_rows}
    except sqlite3.OperationalError:
        positions = {}
    # Condensed: only list sleeves that hold a position; flat sleeves are just a
    # tally (most of the book is flat on any given bar — listing them is noise).
    pos_map = {1: '📈 LONG', -1: '📉 SHORT'}
    active, flat = [], 0
    for r in rows:
        pos = positions.get(r['strategy_id'], r['current_position'] or 0)
        status = r['status'] or 'unknown'
        if pos == 0:
            flat += 1
            continue
        inst = _instrument_from_id(r['strategy_id'])
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
        gt = r["current_gt_score"]
        gt_str = f'{gt:.2f}' if gt is not None else 'n/a'  # None = too few live trades to score yet
        tag = '' if status == 'paper_trading' else f' | {status}'
        active.append(f'  • {inst} {pos_map.get(pos, "?")} | {eq_str} | GT {gt_str}{tag}')
    head = f'📈 <b>Live Paper ({len(rows)} sleeves · {len(active)} in-market)</b>'
    if not active:
        return f'{head}\n  all flat'
    return '\n'.join([head] + active + ([f'  + {flat} flat'] if flat else []))


FIX_STATE_MAX_AGE_H = 24     # older than this and the local file is not the book


def build_fix_section() -> str:
    """In-market prop sleeves, from the LOCAL fix_runner state.

    Skipped when that file is stale. Since the 2026-07-27 Zeabur cutover the prop
    runner writes its state on the POD, so this local copy stops changing and the
    section quietly reports positions from whenever the Mac last ran the book —
    measured 2026-08-09, it was 13 days old and still being printed as current.
    A section that is silently 13 days out of date is worse than no section.
    """
    try:
        if not FIX_STATE_PATH.exists():
            return ''
        age_h = (time.time() - FIX_STATE_PATH.stat().st_mtime) / 3600.0
        if age_h > FIX_STATE_MAX_AGE_H:
            return ''
        state = json.loads(FIX_STATE_PATH.read_text())
    except Exception:
        return ''
    active = []
    for sid, st in state.items():
        if not st.get('pos_id'):
            continue
        inst = sid.split('_auto_')[0].upper()
        side = '📈 LONG' if (st.get('side') or 0) > 0 else '📉 SHORT'
        units = st.get('units')
        active.append(f'  • {inst} {side} | units {units:g}')
    if not active:
        return ''
    note = ''
    try:
        for line in reversed(FIX_LOG_PATH.read_text().splitlines()):
            if 'broker_positions=' in line:
                note = f'  broker snapshot: {line.split("broker_positions=", 1)[1].strip()} open'
                break
    except Exception:
        pass
    head = f'🔧 <b>FIX Prop ({len(active)} in-market)</b>'
    return '\n'.join([head] + active + ([note] if note else []))


def build_report() -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        header = f'📊 <b>Status Report (4h)</b> — {datetime.now().strftime("%H:%M")}'
        research = build_research_section(cur)
        fix = build_fix_section()
    finally:
        conn.close()
    # Account-level PROP drawdown (never let a failure break the report).
    #
    # PROP_GUARD_VENUE is pinned to ctrader here, and that is the whole point of
    # this block. prop_guard defaults to VENUE='oanda', and the pod sets the env
    # var while a local run does not — so this section, headed "Prop Limits", was
    # reporting the OANDA PAPER book's drawdown. Measured 2026-08-09: it showed
    # -1.57% total from a 102,051 start while the funded account was at -0.02%
    # from 100,000. The two venues keep separate state files precisely because
    # their anchors differ; reading the wrong one is not a rounding error, it is
    # the wrong account. Set before the import: prop_guard reads VENUE at module
    # load, and this is the first thing that imports it in this process.
    try:
        os.environ.setdefault('PROP_GUARD_VENUE', 'ctrader')
        import prop_guard
        if prop_guard.VENUE != 'ctrader':
            # Something imported prop_guard before us, so it bound VENUE='oanda'
            # at module load and the setdefault above came too late. Reload
            # rather than silently report the paper book under a "Prop" heading.
            import importlib
            prop_guard = importlib.reload(prop_guard)
        prop = prop_guard.report_section(compact=True)
    except Exception:
        prop = ''
    # Live paper equity and the incubation tracker are DELIBERATELY not here.
    # This report is about the funded account; per-sleeve OANDA equity answered a
    # different question and made the message long enough that the prop line got
    # skimmed. incubation.report_section() still runs on demand
    # (`python incubation.py`) and is the right tool when a sleeve is suspect.
    parts = [header, research] + ([fix] if fix else []) + ([prop] if prop else [])
    return _telegram_safe_html('\n\n'.join(parts))


def main():
    report = build_report()
    ok = notify_html(report)
    print(report)
    print('\n[telegram sent]' if ok else '\n[telegram NOT sent — check token/chat env]')


if __name__ == '__main__':
    main()
