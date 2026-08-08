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
GUARD_UNARMED = 'GUARD_UNARMED'
GUARD_STALE = 'GUARD_STALE'

# ---------------------------------------------------------------------------
# The prop drawdown breaker, watched the same way a sleeve is.
#
# It has now failed SILENTLY three times in two days, each in a different way,
# and all three were found by luck rather than by anything looking:
#
#   * 2026-08-06  ARMED BUT BLIND. get_price re-subscribed per call, spot subs
#     are per-connection, so the 2nd call raised ALREADY_SUBSCRIBED and
#     _fetch_nav_ctrader returned None. It sampled ONCE per pod lifetime while
#     the log still said ARMED (3c56168).
#   * 2026-08-07  SILENTLY DISARMED. A dashboard env edit during an unrelated
#     sleeve swap dropped PROP_GUARD_HALT back to 0. The book traded a full day
#     unprotected while every record in the project said ARMED.
#   * the same day, the consequence: guard_tick returns BEFORE it samples when
#     unarmed, so the state file was not merely coarse, it was FROZEN.
#
# Same shape as the usdchf stall that created this script: a dead guard and a
# healthy one are indistinguishable from outside. The state file's last_updated
# is the positive evidence, and PROP_GUARD_HALT's VALUE is the arming evidence —
# `interlock env` prints names only, and the name is present either way, which
# is precisely why the disarm was invisible.
# ---------------------------------------------------------------------------

# The pod samples every PROP_GUARD_EVERY x TRIGGER_POLL(60) seconds. DERIVED, not
# pinned: this was hard-coded 1800 with a comment reading "PROP_GUARD_EVERY(5) x 60
# = 300s, so 1800s is six missed samples". The 2026-08-08 risk deploy set
# PROP_GUARD_EVERY=1 and the comment silently became false — 1800s is thirty missed
# samples at a 60s cadence. The value stayed SAFE in that direction, but the drift
# runs the other way too: at PROP_GUARD_EVERY=10 (600s) a pinned 1800 is under three
# samples and starts crying stall at a healthy guard.
#
# So it tracks the knob. Two terms, and the FLOOR usually binds:
#   * GUARD_STALE_SAMPLES missed samples — the "is it still ticking" signal.
#   * GUARD_STALE_FLOOR — a trading pass blocks the wait loop for minutes (the FRED
#     fetches alone), and a pass is not a stall. No sample-count reasoning may take
#     the threshold below this or every pass becomes a false alarm.
TRIGGER_POLL_SECONDS = 60
GUARD_STALE_SAMPLES = 6
GUARD_STALE_FLOOR = 1800


def guard_stale_seconds(guard_every=None, floor=GUARD_STALE_FLOOR,
                        samples=GUARD_STALE_SAMPLES, poll=TRIGGER_POLL_SECONDS):
    """Staleness threshold in seconds for a guard sampling every `guard_every` polls.

    Reads PROP_GUARD_EVERY the same way fix_runner does (env, default 5) so the two
    cannot disagree. Never returns less than `floor`.
    """
    if guard_every is None:
        try:
            guard_every = int(os.getenv('PROP_GUARD_EVERY', '5'))
        except ValueError:
            guard_every = 5
    guard_every = max(1, guard_every)
    return max(floor, samples * guard_every * poll)


GUARD_STALE_SECONDS = guard_stale_seconds()
INTERLOCK = os.path.join(ROOT, 'scripts', 'zeabur_interlock.sh')

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

# Loss threshold, DERIVED from the book's own sizing rather than pinned.
#
# CALIBRATED 2026-07-31 against the CORRECTED simulator (commit 58c1a6f — the
# pre-correction series re-entered on unchanged signals and was materially too
# benign, so any threshold set from it would have been too loose). 668 daily
# bars, 2024-01-01 -> 2026-07-30, at RISK_PER_TRADE=0.01: sd 0.90%, 1%ile
# -2.23%, worst -4.71%. Fire rates: -1.0% 18.5/yr, -1.25% 10.2/yr, -1.5%
# 6.0/yr, -2.0% 3.0/yr. -1.5% was chosen because ~6/yr is the rate at which a
# "go and look" prompt still gets looked at; the orphan sweep is the standing
# reminder that a channel crying wolf is worse than no channel.
#
# WHY DERIVED. That calibration fixes a RATIO, not a percentage: -1.5% at
# RISK_PER_TRADE=0.01 is 1.5x base risk, and daily returns scale with base risk
# (measured 2026-08-05: 0.005 -> 0.002 moved the worst day -1.51% -> -0.53%,
# close to linear). Pinning the percentage meant every sizing change silently
# altered the fire rate — twice in one day on 2026-08-05, 0.01 -> 0.005 ->
# 0.002, each time making the watcher rarer without saying so. Silence reads
# exactly like "nothing wrong", which is the failure this script exists to stop.
LOSS_PCT_PER_RISK = 1.5


def _paper_risk():
    """RISK_PER_TRADE the paper book is actually running, or None.

    Read from the .env FILE, not os.getenv: run_book_watch.sh sources ~/.zshrc
    but NOT .env, so under launchd the variable is simply absent (verified
    2026-08-05). A getenv default would quietly re-create the desync this whole
    mechanism exists to prevent — so the file, which is what live_test is
    started with, is the source of truth.
    """
    val = os.getenv('RISK_PER_TRADE')
    if not val:
        try:
            for line in open(os.path.join(ROOT, '.env')):
                line = line.strip()
                if line.startswith('RISK_PER_TRADE='):
                    val = line.split('=', 1)[1].strip().strip('"\'')
                    break
        except OSError:
            return None
    try:
        return float(val) if val else None
    except ValueError:
        return None


_PAPER_RISK = _paper_risk()
if _PAPER_RISK:
    LOSS_PCT = LOSS_PCT_PER_RISK * _PAPER_RISK
else:
    LOSS_PCT = 0.0075                       # 1.5 x 0.005, the standard sizing
    print('book_watch: RISK_PER_TRADE not resolvable — loss threshold pinned at '
          f'{LOSS_PCT*100:.2f}%, which may not match the book', file=sys.stderr)

# THE FIRE RATE IS A SIMULATED ONE. It is what this book would have done over
# 2.5 years, not what it will do, and the live series (sleeve_equity, from
# 2026-07-29) is still far too short to check it against. Revisit once there
# are enough live bars to compare, NOT before.


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


def guard_findings(armed, last_updated, error, now, max_age=GUARD_STALE_SECONDS):
    """Pure. -> [(code, sleeve_id, key, detail)] for the prop breaker.

    `key` is the dedup column, and it is chosen per failure. A stalled guard
    keeps reporting the same frozen last_updated, so the row collides and it
    shouts ONCE per episode. UNARMED keys on the date and nags DAILY on purpose:
    it is the state that ran a full day unnoticed on 2026-08-07, and one alert
    you happen to miss puts you straight back there.

    A probe FAILURE yields NO finding. Unreachable is not a breach, and alerting
    on every flaky SSH round trip is how an alert gets muted — the caller prints
    it instead, so it stays in the log without spending the Telegram budget.
    Note what this trades away: a guard that is unreadable for days is not
    reported, so the log is the only place that distinguishes "healthy" from
    "never actually checked".
    """
    if error:
        return []

    day = now.strftime('%Y-%m-%d')
    out = []
    if armed is False:
        out.append((GUARD_UNARMED, '', day,
                    'PROP_GUARD_HALT is not 1 on the pod — the drawdown breaker is '
                    'DISARMED and the book is trading unprotected. It silently '
                    'reverted once already (2026-08-07) via a dashboard env edit.'))
    if not last_updated:
        out.append((GUARD_STALE, '', day,
                    'the guard has no state file on the volume — it has never '
                    'sampled, or it is writing somewhere ephemeral again.'))
        return out

    try:
        seen = datetime.fromisoformat(last_updated)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except Exception:
        return out + [(GUARD_STALE, '', str(last_updated),
                       f'unparseable guard timestamp {last_updated!r}')]

    age = (now - seen).total_seconds()
    if age > max_age:
        out.append((GUARD_STALE, '', last_updated,
                    f'the guard last sampled {age/60:.0f} min ago '
                    f'({last_updated}), over the {max_age/60:.0f} min threshold. '
                    f'A breaker that stopped looking reads exactly like one that '
                    f'sees nothing wrong.'))
    return out


def probe_guard(script=INTERLOCK, timeout=180):
    """-> (armed, last_updated, error, guard_every). Shells out to the interlock
    script, which owns the SSH credentials; this stays a reader.

    Returns (None, None, reason, None) when the probe cannot run at all — a missing
    script or absent SSH config means "not the ops machine", which the caller
    treats as skip, not as a finding.

    guard_every is the POD's PROP_GUARD_EVERY, not this machine's. The staleness
    threshold derives from the sampling cadence, and the cadence is a pod env var —
    reading it locally would silently score the pod against a default it does not
    run. None means the pod did not report it; the caller then falls back.
    """
    import json as _json
    import subprocess
    if not os.path.exists(script):
        return None, None, 'SKIP', None
    try:
        env_txt = open(os.path.join(ROOT, '.env')).read()
    except Exception:
        return None, None, 'SKIP', None
    if 'IP=' not in env_txt or 'USERNAME=' not in env_txt:
        return None, None, 'SKIP', None

    def _run(sub):
        p = subprocess.run(['bash', script, sub], cwd=ROOT, timeout=timeout,
                           capture_output=True, text=True)
        return p.stdout.replace('\r', '')

    try:
        risk = _run('risk')
        armed = None
        guard_every = None
        for line in risk.splitlines():
            if line.startswith('PROP_GUARD_HALT='):
                armed = line.split('=', 1)[1].strip() == '1'
            elif line.startswith('PROP_GUARD_EVERY='):
                try:
                    guard_every = int(line.split('=', 1)[1].strip())
                except ValueError:
                    guard_every = None
        state = _run('guard-state')
        last = None
        if '{' in state:
            try:
                last = _json.loads(state[state.index('{'):state.rindex('}') + 1]) \
                             .get('last_updated')
            except Exception:
                last = None
        if armed is None and last is None:
            return None, None, 'the pod returned neither PROP_GUARD_HALT nor a state file', None
        return armed, last, None, guard_every
    except subprocess.TimeoutExpired:
        return None, None, f'interlock probe timed out after {timeout}s', None
    except Exception as e:
        return None, None, repr(e)


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
    ap.add_argument('--no-guard', action='store_true',
                    help='skip the prop-breaker probe (it costs one SSH round trip)')
    ap.add_argument('--guard-stale', type=int, default=None,
                    metavar='MIN', help='alert when the guard has not sampled for this '
                                        'many minutes. Default DERIVES from the pod\'s '
                                        'PROP_GUARD_EVERY (%d min at the current '
                                        'default); pass a value to override.'
                                        % (GUARD_STALE_SECONDS // 60))
    ap.add_argument('--guard-timeout', type=int, default=180,
                    help='seconds to allow the remote probe')
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

    # NO prop-equivalent figure. It used to say "paper is 2x the prop book, so
    # halve this" — true only while RISK_PER_TRADE happened to be double
    # BASE_RISK. They are independent knobs and diverged on 2026-08-05, at which
    # point the alert was understating the prop book by 2x while sounding
    # precise. This script can see the paper book's sizing and NOT the pod's, so
    # it reports what it measured and says where to look for the rest.
    findings = [(BOOK_LOSS, '', bt,
                 f'Paper book lost {p:,.2f} USD ({frac*100:+.2f}% of '
                 f'{NOMINAL_EQUITY:,.0f} nominal) on the bar closing {bt}, at '
                 f'RISK_PER_TRADE={_PAPER_RISK or "?"}. For the prop-book '
                 f'equivalent scale by its BASE_RISK '
                 f'(./scripts/zeabur_interlock.sh risk) — this script cannot see it.')
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

    if not a.no_guard:
        armed, last_updated, err, guard_every = probe_guard(timeout=a.guard_timeout)
        if err == 'SKIP':
            print('  prop guard: not probed (no interlock script / SSH config here)')
        elif err:
            # Log-only by choice: a probe failure is not a breach. Worded so a
            # reader cannot mistake it for a clean check.
            print(f'  prop guard: NOT READ ({err}) — armed-ness and sampling are '
                  f'UNKNOWN, which is not the same as healthy. Not alerted.')
        else:
            # An explicit --guard-stale wins; otherwise derive from the cadence the
            # POD reports, so changing PROP_GUARD_EVERY cannot leave this scoring
            # against a stale assumption.
            max_age = (a.guard_stale * 60 if a.guard_stale is not None
                       else guard_stale_seconds(guard_every))
            g = guard_findings(armed, last_updated, err,
                               datetime.now(timezone.utc), max_age)
            if not g:
                print(f'  prop guard: ARMED, last sampled {last_updated} '
                      f'(stale threshold {max_age // 60} min'
                      + (f', PROP_GUARD_EVERY={guard_every}' if guard_every else '')
                      + ')')
            findings += g

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
        icon = {BOOK_LOSS: '🩸', SLEEVE_STALE: '🕳', GUARD_UNARMED: '🛡',
                GUARD_STALE: '🛡'}.get(code, '🚫')
        what = {BOOK_LOSS: 'bad day', SLEEVE_STALE: 'stopped evaluating',
                GUARD_UNARMED: 'DRAWDOWN BREAKER DISARMED',
                GUARD_STALE: 'drawdown breaker stopped sampling'}.get(
            code, 'never passed validation')
        label = 'prop guard' if code.startswith('GUARD_') else (
            'book' if not sid else sid.split('_auto_')[0])
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
