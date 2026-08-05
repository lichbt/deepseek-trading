#!/usr/bin/env python3
"""Watch the live book for the two failures that have gone unnoticed.

WHY THIS EXISTS. Two independent silent failures surfaced within two days
(2026-07-30/31), and in BOTH cases the data to catch them was already in
pipeline.db and nothing was looking:

  * 2026-07-30 the paper book lost 3,978 USD in one session, -3.34% of NAV.
    Nothing reported it. The 4-hourly report shows balances, decay_watch
    reports verdict flips; neither says "today was bad".
  * usdchf_i21 evaluated ZERO bars from 07-12 to 07-22 while 24 of the other
    25 sleeves evaluated all nine. It held a position the whole time, and
    because live_test's software stop is enforced INSIDE that same loop, the
    position was unstopped for ten days. It looked completely healthy.

The second is the reason this exists at all. A sleeve that stops evaluating is
indistinguishable from a healthy one from the outside — no error, no missing
process, no alert — and it is the failure mode with real money attached.

WHAT IT DOES NOT DO: retire, resize, flatten or trade. Read-only against the
book, append-only against the DB. Every finding is a prompt to go and look.

    ./venv/bin/python scripts/book_watch.py                 # record + alert
    ./venv/bin/python scripts/book_watch.py --dry-run       # print only
    ./venv/bin/python scripts/book_watch.py --replay 14     # last 14 book bars,
                                                            # writes nothing
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'pipeline.db')

BOOK_LOSS = 'BOOK_LOSS'
SLEEVE_STALE = 'SLEEVE_STALE'
SLEEVE_RESUMED = 'SLEEVE_RESUMED'
SLEEVE_UNVALIDATED = 'SLEEVE_UNVALIDATED'

# Statuses a sleeve is allowed to have been deployed FROM. Anything else in its
# status_history means a gate rejected it at some point and it reached the book
# anyway. Kept as a literal rather than imported from pipeline_utils so this
# script stays runnable against a copied DB with no repo import path.
GATE_FAILURES = ('research_failed', 'walk_forward_failed', 'holdout_failed')

# A bar_time counts as a BOOK BAR only when at least this fraction of live
# sleeves recorded it. Instruments do not share a calendar — BTC and ETH trade
# weekends while FX and the indices do not — so a naive "every distinct
# bar_time" reference would mark the whole FX book two bars stale every Monday.
# Requiring a quorum means a crypto-only weekend bar is simply not a book bar.
BOOK_BAR_QUORUM = 0.5

# Bars behind the book before a sleeve is called stale. 2 is one missed session
# plus a bar of slack; usdchf's stall reached 9.
STALE_BARS = 3

# Nominal equity for expressing the loss as a percentage. DELIBERATELY a fixed
# base and not a mark-to-market NAV: live_status.equity_curve is known corrupt
# (one account balance copied into all sleeves, then truncated on every
# restart), so the DB holds no trustworthy equity series to divide by. The
# USD figure is the measurement; the percentage is an aid to reading it.
NOMINAL_EQUITY = 100_000.0

# Loss threshold as a fraction of NOMINAL_EQUITY.
#
# RECALIBRATED 2026-08-05, RISK_PER_TRADE 0.01 -> 0.005. The paper book used to
# run DOUBLE the prop book's base risk, and the threshold below was set on a
# series doubled to match. At equal sizing the series is half what it was, so a
# -1.5% day now needs what used to be a -3.0% day — off the bottom of the table
# below, roughly once a year. The watcher would have gone quiet without saying
# so, which is the worst way for a monitor to fail. Halved to hold the fire rate.
#
# CALIBRATED 2026-07-31 against the CORRECTED simulator (commit 58c1a6f — the
# pre-correction series re-entered on unchanged signals and was materially too
# benign, so any threshold set from it would have been too loose). 668 daily
# bars, 2024-01-01 -> 2026-07-30, AT DOUBLE sizing: sd 0.90%, 1%ile -2.23%,
# worst -4.71%. Fire rates: -1.0% 18.5/yr, -1.25% 10.2/yr, -1.5% 6.0/yr,
# -2.0% 3.0/yr. ~6/yr is the rate at which a "go and look" prompt still gets
# looked at; the orphan sweep is the standing reminder that a channel crying
# wolf is worse than no channel. Halve every figure above for today's sizing.
#
# THE PROP BOOK IS NO LONGER A FIXED MULTIPLE OF THIS ONE. It runs BASE_RISK
# (0.002 while the new account is on trial, 0.005 normally) and this book runs
# RISK_PER_TRADE, two independent knobs — so do NOT read a percentage here as a
# prop-book percentage without checking both.
#
# THE FIRE RATE IS A SIMULATED ONE. It is what this book would have done over
# 2.5 years, not what it will do, and the live series (sleeve_equity, from
# 2026-07-29) is still far too short to check it against. Revisit once there
# are enough live bars to compare, NOT before.
LOSS_PCT = 0.0075


# ---------------------------------------------------------------------------
# Pure logic — no DB, no I/O, no clock. All of the silence rules live here.
# ---------------------------------------------------------------------------
def book_bars(rows, n_live, quorum=BOOK_BAR_QUORUM):
    """Ordered bar_times where a quorum of live sleeves reported.

    `rows` is [(bar_time, sleeve_id), ...]. Returns the reference calendar the
    staleness check is measured against — derived from what the book actually
    did, not from a hard-coded market calendar, so it needs no holiday table
    and cannot drift at DST.

    LOAD-BEARING ASSUMPTION: bar_time sorts lexicographically into chronological
    order. live_test writes str(current_bar_time), i.e. ISO-8601
    '2026-07-29 21:00:00+00:00', for which that holds. Anything writing a
    different format would silently reorder the calendar rather than fail.
    """
    if n_live <= 0:
        return []
    seen = {}
    for bar_time, sleeve_id in rows:
        seen.setdefault(bar_time, set()).add(sleeve_id)
    need = max(1, int(n_live * quorum))
    return sorted(bt for bt, sids in seen.items() if len(sids) >= need)


def stale_sleeves(bars, last_seen, live, threshold=STALE_BARS):
    """[(sleeve_id, last_bar, n_behind)] for live sleeves lagging the book.

    Silence rules, each one keeping the channel believable:

      * Only live sleeves. A retired sleeve stops writing rows BY DESIGN and
        would otherwise alert forever — the orphan-sweep lesson.
      * A sleeve with NO rows at all is not reported here. It is
        indistinguishable from one deployed an hour ago, and inventing an
        alarm for that trains you to ignore the real ones. main() lists those
        separately, without alerting.
      * Lag is counted in BOOK BARS, not calendar days, so weekends, holidays
        and DST cannot manufacture a finding.
    """
    out = []
    index = {bt: i for i, bt in enumerate(bars)}
    for sid in sorted(live):
        last = last_seen.get(sid)
        if last is None:
            continue
        if last not in index:
            # The sleeve's newest bar is not a book bar (e.g. a crypto-only
            # weekend bar). Count the book bars strictly after it instead.
            behind = sum(1 for bt in bars if bt > last)
        else:
            behind = len(bars) - 1 - index[last]
        if behind >= threshold:
            out.append((sid, last, behind))
    return out


def losing_bars(pnl_by_bar, equity=NOMINAL_EQUITY, pct=LOSS_PCT):
    """[(bar_time, pnl, pct_of_equity)] for bars worse than the threshold.

    `pnl_by_bar` is [(bar_time, summed_sleeve_pnl), ...]. Bars whose P&L is
    None are SKIPPED, never treated as zero: sleeve_pnl is NULL on rows that
    predate the currency columns and on every log-backfilled row, and reading
    "no data" as "flat" is exactly how a missing bar becomes an invisible one.
    """
    limit = -abs(equity * pct)
    return [(bt, p, p / equity) for bt, p in pnl_by_bar
            if p is not None and p <= limit]


def unvalidated_sleeves(live, results, histories):
    """Live sleeves whose provenance does not support being on the book.

    Two separable defects, both real, reported together because the question
    they answer is one question — "did this sleeve earn its place?":

      * validation_results says something other than PASS (or says nothing);
      * validation_results says PASS but status_history records a gate
        rejection, i.e. the two disagree about the same run.

    The second is the one that hid wticousd_auto_20260527_105800_i13 for ten
    weeks. Its row reads "PASS (D)" with empty torture flags while its history
    records "FAIL: directional_bias(one_sided=long)" 1.2 ms later, so every
    reader of the validation artifact — evaluate_strategy, hourly_report, a
    human — saw a clean pass.

    `results` maps sid -> final_status (None when the row is missing);
    `histories` maps sid -> [(new_status, reason), ...] in order.
    """
    out = []
    for sid in sorted(live):
        final = results.get(sid)
        hist = histories.get(sid) or []
        rejects = [(st, why) for st, why in hist if st in GATE_FAILURES]
        if final is None:
            out.append((sid, 'no validation_results row at all', ''))
        elif not final.lower().startswith('pass'):
            out.append((sid, 'validation_results does not say PASS', final))
        elif rejects:
            st, why = rejects[-1]
            out.append((sid, f'validation_results says PASS but history records {st}', why))
    return out


def suppress_recorded(findings, recorded):
    """Drop findings already announced. `recorded` is {(code, sleeve, bar)}.

    Dedup is ALSO enforced by the UNIQUE constraint on book_events, so this is
    belt-and-braces — but it has to happen here too, because the DB constraint
    silences the INSERT and not the Telegram message.
    """
    return [f for f in findings if (f[0], f[1], f[2]) not in recorded]


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------
def live_sleeves(conn):
    return {r[0] for r in conn.execute(
        "SELECT id FROM strategies WHERE status='paper_trading'")}


def equity_rows(conn, live):
    if not live:
        return [], {}, []
    marks = ','.join('?' * len(live))
    rows = conn.execute(
        f"SELECT bar_time, sleeve_id FROM sleeve_equity WHERE sleeve_id IN ({marks})",
        tuple(live)).fetchall()
    last_seen = {}
    for bar_time, sid in rows:
        if sid not in last_seen or bar_time > last_seen[sid]:
            last_seen[sid] = bar_time
    # The loss sum is deliberately NOT restricted to currently-live sleeves.
    # What the book lost on a bar is what everything trading that bar lost;
    # scoping to today's roster makes a retirement retroactively rewrite
    # history. Measured 2026-07-31: the 07-29 bar reads -1,552.92 across the
    # 25 survivors and -3,978.55 across the 27 that actually traded it, the
    # whole difference being the since-retired nas100 0728 sleeve. At ALARM
    # time the two agree — a sleeve is still live the day after its bad day —
    # so this only ever changes the replay, and only toward the truth.
    pnl = conn.execute(
        "SELECT bar_time, SUM(sleeve_pnl) FROM sleeve_equity "
        "GROUP BY bar_time ORDER BY bar_time").fetchall()
    return rows, last_seen, pnl


def provenance_rows(conn, live):
    """final_status and status_history for each live sleeve."""
    if not live:
        return {}, {}
    marks = ','.join('?' * len(live))
    results = {sid: fs for sid, fs in conn.execute(
        f'SELECT strategy_id, final_status FROM validation_results '
        f'WHERE strategy_id IN ({marks})', tuple(live))}
    histories = {}
    for sid, new_status, reason in conn.execute(
            f'SELECT strategy_id, new_status, reason FROM status_history '
            f'WHERE strategy_id IN ({marks}) ORDER BY id', tuple(live)):
        histories.setdefault(sid, []).append((new_status, reason or ''))
    return results, histories


def recorded_events(conn):
    return {(c, s, b) for c, s, b in conn.execute(
        "SELECT event_code, sleeve_id, bar_time FROM book_events")}


def record(conn, code, sleeve_id, bar_time, detail):
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "INSERT OR IGNORE INTO book_events "
        "(occurred_at, event_code, sleeve_id, bar_time, detail) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), code, sleeve_id, bar_time, detail))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='print findings, write nothing, send nothing')
    ap.add_argument('--replay', type=int, metavar='N',
                    help='report every finding over the last N book bars '
                         'ignoring what was already announced; implies --dry-run')
    ap.add_argument('--stale-bars', type=int, default=STALE_BARS)
    ap.add_argument('--loss-pct', type=float, default=LOSS_PCT * 100,
                    help='alert below this %% of nominal equity (default 1.5)')
    ap.add_argument('--db', default=DB)
    a = ap.parse_args()
    dry = a.dry_run or a.replay is not None

    conn = sqlite3.connect(a.db)
    live = live_sleeves(conn)
    rows, last_seen, pnl = equity_rows(conn, live)
    bars = book_bars(rows, len(live))

    if not bars:
        print('book_watch: no book bars recorded yet — nothing to check')
        return

    window = bars[-a.replay:] if a.replay else bars
    losses = losing_bars([(bt, p) for bt, p in pnl if bt in set(window)],
                         pct=a.loss_pct / 100)
    stale = stale_sleeves(bars, last_seen, live, a.stale_bars)

    findings = [(BOOK_LOSS, '', bt,
                 f'Book lost {p:,.2f} USD ({frac*100:+.2f}% of {NOMINAL_EQUITY:,.0f} '
                 f'nominal) on the bar closing {bt}. Paper sizing is 2x the prop '
                 f'book, so the prop-equivalent is roughly {frac*50:+.2f}%.')
                for bt, p, frac in losses]
    findings += [(SLEEVE_STALE, sid, last,
                  f'{sid} has recorded no bar since {last} — {behind} book bars '
                  f'behind. It may still hold a position, and live_test enforces '
                  f'its software stop inside the same loop, so treat it as '
                  f'UNSTOPPED until checked. Read its log and its broker position.')
                 for sid, last, behind in stale]

    # Provenance is not tied to a bar, so it is keyed on the sleeve's own last
    # bar (or '' when never observed) purely to give suppress_recorded a stable
    # key — it must announce once, not every four hours forever.
    results, histories = provenance_rows(conn, live)
    findings += [(SLEEVE_UNVALIDATED, sid, '',
                  f'{sid} is on the book but {problem}: {detail!r}. It is trading '
                  f'real size on a validation record that does not support it. '
                  f'This is a provenance failure, not a performance one — check '
                  f'how it was deployed before judging its P&L.')
                 for sid, problem, detail in
                 unvalidated_sleeves(live, results, histories)]

    if not a.replay:
        findings = suppress_recorded(findings, recorded_events(conn))

    never = sorted(sid for sid in live if sid not in last_seen)

    print(f'book_watch: {len(live)} live sleeves, {len(bars)} book bars '
          f'({bars[0]} -> {bars[-1]})')
    if never:
        print(f'  not yet observed ({len(never)}), NOT alerted — indistinguishable '
              f'from a fresh deploy: {", ".join(s.split("_auto_")[0] for s in never)}')
    if not findings:
        print('  no findings')
        return

    lines = []
    for code, sid, bar, detail in findings:
        icon = {BOOK_LOSS: '🩸', SLEEVE_STALE: '🕳'}.get(code, '🚫')
        what = {BOOK_LOSS: 'bad day', SLEEVE_STALE: 'stopped evaluating'}.get(
            code, 'never passed validation')
        label = 'book' if not sid else sid.split('_auto_')[0]
        lines.append(f'{icon} {label} — {what}')
        print(f'  {code}: {detail}')
        if not dry:
            record(conn, code, sid, bar, detail)

    if dry:
        print(f'\n({"replay" if a.replay else "dry run"} — nothing written, nothing sent)')
        return

    try:
        from telegram_bot import notify_html
        notify_html('<b>Book watch</b>\n' + '\n'.join(lines) +
                    '\n\nNothing was resized or closed. Go and look.')
    except Exception as e:
        # The DB rows are the durable record; a failed send must not lose them.
        print(f'WARNING: alert not sent ({e}) — findings ARE recorded', file=sys.stderr)


if __name__ == '__main__':
    main()
