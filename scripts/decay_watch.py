#!/usr/bin/env python3
"""Watch the live book's decay verdicts, record flips, and alert on change.

WHY THIS EXISTS. Decay detection has always been automatic — portfolio.py
recomputes recent_decay_status for every sleeve on each run (daily, 00:05). What
was missing was anyone finding out. Nothing in telegram_bot or the 4-hourly
report mentions decay, so a DECAYED verdict resized a sleeve with its only trace
in one of 25 per-sleeve log files.

That got worse, not better, when live_test started re-reading portfolio_state.json
every bar: the halving (0.5x conviction AND 0.5x decay_kelly_scale = 0.25x
combined) now applies with no restart, i.e. no human involved at any point. Silent
automatic resizing is the failure mode this repo has paid for repeatedly.

WHAT IT DOES NOT DO: retire anything. Detection halves size; retiring is a human
decision made against a fresh reconstruction (see .claude/skills/sleeve-ops).
This is read-only against the book and append-only against the DB.

    ./venv/bin/python scripts/decay_watch.py            # record + alert on change
    ./venv/bin/python scripts/decay_watch.py --dry-run  # print, write nothing
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reason_codes
import pipeline_utils as pu

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'pipeline.db')


def current_verdicts(conn):
    """{sid: (status, note)} for live sleeves, from portfolio_state.json.

    Read from the state file rather than recomputing, deliberately: this must
    report the verdict the BOOK IS ACTING ON, not a fresh one that may differ.
    A watcher that recomputes can disagree with what is actually sizing trades.
    """
    import json
    state_path = os.path.join(os.path.dirname(DB), 'portfolio_state.json')
    state = json.load(open(state_path))
    notes = state.get('decay_note', {})
    return ({sid: (st, notes.get(sid, '')) for sid, st in state.get('decay_status', {}).items()},
            state.get('generated_at', ''))


def last_recorded(conn):
    """{sid: status} from the newest decay event per sleeve.

    strategy_events IS the previous state — no side-car file to drift out of sync,
    and the history is preserved because the table is append-only.
    """
    rows = conn.execute("""
        SELECT strategy_id, reason_code FROM strategy_events e
        WHERE reason_code IN (?, ?)
          AND occurred_at = (SELECT MAX(occurred_at) FROM strategy_events x
                             WHERE x.strategy_id = e.strategy_id
                               AND x.reason_code IN (?, ?))
    """, (reason_codes.DECAY_DETECTED, reason_codes.DECAY_CLEARED,
          reason_codes.DECAY_DETECTED, reason_codes.DECAY_CLEARED)).fetchall()
    return {sid: ('DECAYED' if code == reason_codes.DECAY_DETECTED else 'OK')
            for sid, code in rows}


def compute_flips(verdicts, previous, live):
    """[(sid, reason_code, prose)] for sleeves whose decay verdict CHANGED.

    Pure: no DB, no I/O. Three rules that each exist to keep the channel quiet
    enough to be believed —

      * INSUFFICIENT is never a flip. It is the measure DECLINING TO SCORE (13 of
        25 sleeves currently), not a verdict. Alerting on it would fire every time
        a sleeve crosses the entry threshold in either direction.
      * A first observation of OK is not a recovery. Otherwise the first run
        announces every healthy sleeve in the book.
      * Only live sleeves. A retired sleeve keeps a stale verdict in the state
        file and must not generate alerts forever.
    """
    out = []
    for sid, (status, note) in sorted(verdicts.items()):
        if sid not in live or status == 'INSUFFICIENT':
            continue
        was = previous.get(sid)
        if was == status:
            continue
        if status == 'DECAYED':
            out.append((sid, reason_codes.DECAY_DETECTED,
                        f'DECAY DETECTED ({was or "first observation"} -> DECAYED): {note}. '
                        f'Auto-sized to 0.25x (0.5x conviction x 0.5x decay_kelly_scale). '
                        f'Retire-or-keep is a HUMAN decision — reconstruct before acting.'))
        elif status == 'OK' and was == 'DECAYED':
            out.append((sid, reason_codes.DECAY_CLEARED,
                        f'DECAY CLEARED (DECAYED -> OK): {note}. Size restored to 1.0x.'))
    return out


def record(conn, sid, code, prose):
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        INSERT INTO strategy_events
            (strategy_id, occurred_at, old_status, new_status, reason_code, reason_prose, source, history_id)
        VALUES (?, ?, NULL, NULL, ?, ?, 'live', NULL)
    """, (sid, datetime.now(timezone.utc).isoformat(), code, prose))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='print, write nothing, send nothing')
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    verdicts, generated = current_verdicts(conn)
    previous = last_recorded(conn)

    live = {r['id'] for r in conn.execute(
        "SELECT id FROM strategies WHERE status='paper_trading'").fetchall()} \
        if conn.row_factory else {r[0] for r in conn.execute(
            "SELECT id FROM strategies WHERE status='paper_trading'").fetchall()}

    flips = compute_flips(verdicts, previous, live)

    if not flips:
        print(f'decay_watch: no change ({len(verdicts)} verdicts, state generated {generated})')
        return

    lines = []
    for sid, code, prose in flips:
        icon = '⚠️' if code == reason_codes.DECAY_DETECTED else '✅'
        short = sid.split('_auto_')[0]
        lines.append(f'{icon} {short} — {"DECAYED" if code == reason_codes.DECAY_DETECTED else "recovered"}')
        print(f'{code}: {sid}\n    {prose}')
        if not a.dry_run:
            record(conn, sid, code, prose)

    if a.dry_run:
        print('\n(dry run — nothing written, nothing sent)')
        return

    try:
        from telegram_bot import notify_html
        notify_html('<b>Decay change</b>\n' + '\n'.join(lines) +
                    '\n\nSleeves are auto-resized already. Retire-or-keep needs a review.')
    except Exception as e:
        # The DB note is the durable record; a failed send must not lose it.
        print(f'WARNING: alert not sent ({e}) — events ARE recorded', file=sys.stderr)


if __name__ == '__main__':
    main()
