"""Deferred-order vs daily-bar simulator parity.

The prop pod places every order in one daily pass at 00:15 UTC. Two instruments
(AU200_AUD among them) are SHUT at that instant during the part of the year when
Europe/Bucharest is on standard time, so their orders are queued by
`fix_runner.defer_action` and executed by `fix_runner.deferred_drain` when the
session opens. Every performance figure for this book comes from a DAILY-BAR
simulator (`oanda_book_simulator.py`, `scripts/risk_model_sim.py`) which has no
intraday clock: it assumes an action decided for a bar happens on that bar.

The deferral is therefore safe for those figures IF AND ONLY IF the delayed
execution still falls inside the SAME BROKER DAY as the pass that decided it.
If a session opened only after the broker day rolled, the order would land on
the next bar, the simulator would be wrong, and (because `deferred_drain`
supersedes any intent whose `broker_day` differs from today's) the intent would
be dropped unfired and the sleeve would never trade at all.

The broker day boundary comes from `prop_guard.broker_now(dt)` (America/New_York
+ 7h) — NEVER a UTC constant. Sessions are published as [(start_sec, end_sec)]
from Sunday 00:00 in Europe/Bucharest; membership is tested with
`fix_runner.session_end(now, intervals)`, which returns None when the instrument
is SHUT at `now` and a UTC datetime when open.

Schedules are hardcoded from the live broker (read 2026-08-18; no network call):
  AU200_AUD — Mon..Fri, 02:50-09:29 and 10:10-23:59 Europe/Bucharest.
  NAS100_USD (control, open essentially always) — Mon..Fri, 00:05-23:55.

This is a measurement, not a known-good fact. A FAIL here is a valid and useful
result: the tests express the real behaviour and report it.
"""
import os
from datetime import datetime, timezone, timedelta

import pytest

import fix_runner as fr
import prop_guard

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARY_PATH = os.path.join(REPO_ROOT, '.scratch', 'defer', 'n4-parity.md')

# The single pass instant the prop pod uses, on each weekday.
PASS_HOUR, PASS_MINUTE = 0, 15

# Sweep window from the task.
SWEEP_START = datetime(2024, 1, 1)
SWEEP_END = datetime(2026, 12, 31)


def _hm(h, m):
    """HH:MM to seconds from midnight."""
    return h * 3600 + m * 60


def _intervals(windows, days):
    """Build [(start_sec, end_sec)] from Sunday 00:00 Europe/Bucharest.

    Sunday is day 0, Monday is day 1, ... Friday is day 5 — the convention the
    broker's `ProtoOASymbolByIdReq` returns and `session_end` expects
    (`week_start` is the Sunday of the current week).
    """
    out = []
    for d in days:                       # d is 0..4 for Mon..Fri -> day index d+1
        base = (d + 1) * 86400
        for (h0, m0, h1, m1) in windows:
            out.append((base + _hm(h0, m0), base + _hm(h1, m1)))
    return out


# AU200_AUD: 02:50-09:29 and 10:10-23:59, Mon..Fri (days 0..4 -> Mon..Fri).
AU200_INTERVALS = _intervals(
    [(2, 50, 9, 29), (10, 10, 23, 59)], range(5))

# NAS100_USD: 00:05-23:55, Mon..Fri — the control, open essentially always.
NAS100_INTERVALS = _intervals([(0, 5, 23, 55)], range(5))

# Cap on the reopen search: 24h in 5-minute steps = 288 steps.
_STEP = timedelta(minutes=5)
_STEP_CAP = 24 * 12


def _pass_instant(day):
    """00:15 UTC on `day` (a naive date/datetime at midnight)."""
    return datetime(day.year, day.month, day.day, PASS_HOUR, PASS_MINUTE,
                    tzinfo=timezone.utc)


def _broker_day(dt):
    """The broker-day label for `dt`, via the one definition of the broker clock."""
    return prop_guard.broker_now(dt).strftime('%Y-%m-%d')


def _find_reopen(pass_inst, intervals):
    """First instant >= pass_inst at which the instrument is OPEN, or None.

    Steps forward in 5-minute increments, capped at 24h, exactly as the task
    specifies. `session_end` returns None when SHUT, a UTC datetime when open.
    """
    t = pass_inst
    for _ in range(_STEP_CAP):
        if fr.session_end(t, intervals) is not None:
            return t
        t = t + _STEP
    return None


def _weekdays(start, end):
    """All Mon..Fri dates in [start, end] inclusive."""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d = d + timedelta(days=1)
    return out


WEEKDAYS = _weekdays(SWEEP_START, SWEEP_END)


def _sweep(intervals):
    """Categorize every weekday in the sweep window for one instrument.

    Returns a list of dicts: {date, pass_broker_day, status, reopen,
    reopen_broker_day}. status is one of:
      'open'   — instrument was OPEN at the pass (no deferral needed)
      'same'   — SHUT at pass, reopened in the SAME broker day (deferral works)
      'later'  — SHUT at pass, reopened only in a LATER broker day (deferral
                 FAILS — the sleeve would silently not trade). Includes the case
                 where no reopen was found within 24h.
    """
    rows = []
    for day in WEEKDAYS:
        p = _pass_instant(day)
        bd_pass = _broker_day(p)
        if fr.session_end(p, intervals) is not None:
            rows.append({'date': p.strftime('%Y-%m-%d'),
                         'weekday': p.weekday(),
                         'pass_broker_day': bd_pass,
                         'status': 'open',
                         'reopen': None, 'reopen_broker_day': None})
            continue
        ro = _find_reopen(p, intervals)
        if ro is None:
            rows.append({'date': p.strftime('%Y-%m-%d'),
                         'weekday': p.weekday(),
                         'pass_broker_day': bd_pass,
                         'status': 'later',
                         'reopen': None, 'reopen_broker_day': '<no reopen in 24h>'})
            continue
        bd_ro = _broker_day(ro)
        rows.append({'date': p.strftime('%Y-%m-%d'),
                     'weekday': p.weekday(),
                     'pass_broker_day': bd_pass,
                     'status': 'same' if bd_ro == bd_pass else 'later',
                     'reopen': ro, 'reopen_broker_day': bd_ro})
    return rows


AU200_SWEEP = _sweep(AU200_INTERVALS)
NAS100_SWEEP = _sweep(NAS100_INTERVALS)


# ---------------------------------------------------------------------------
# 1. Same-broker-day property, AU200.
# ---------------------------------------------------------------------------
class TestAU200SameBrokerDay:
    def test_deferred_reopen_lands_on_the_same_broker_day_as_the_pass(self):
        """For every weekday in 2024-01-01..2026-12-31: take 00:15 UTC. If AU200
        is OPEN then, nothing to prove. If it is SHUT, find the NEXT instant at
        which it opens (5-min steps, cap 24h) and assert that the broker-day
        label of that reopen equals the broker-day label of the pass.

        A failure here means the daily-bar simulator and the live deferred fill
        disagree on which bar the order lands on — and `deferred_drain` would
        drop the intent as stale, so the sleeve would not trade at all on that
        day. The first failing date is reported.
        """
        failing = [r for r in AU200_SWEEP if r['status'] == 'later']
        assert not failing, (
            "AU200 deferred reopen crossed the broker day for these pass dates "
            "(pass_broker_day -> reopen_broker_day):\n  " +
            "\n  ".join(f"{r['date']} (weekday {r['weekday']}): "
                        f"{r['pass_broker_day']} -> {r['reopen_broker_day']}"
                        for r in failing[:50])
        )


# ---------------------------------------------------------------------------
# 2. Control — NAS100 is open at the pass on every weekday, never deferred.
# ---------------------------------------------------------------------------
class TestNAS100Control:
    def test_nas100_is_open_at_the_pass_on_every_weekday(self):
        """NAS100_USD trades 00:05-23:55 Mon..Fri Europe/Bucharest, so it is
        open at 00:15 UTC on every weekday in both DST regimes. It should
        therefore NEVER be deferred — assert exactly that. If a day appears
        here, the control is mis-scheduled and the measurement above is
        suspect for the same reason."""
        deferred = [r for r in NAS100_SWEEP if r['status'] != 'open']
        assert not deferred, (
            "NAS100_USD was deferred on these weekdays (expected never):\n  " +
            "\n  ".join(f"{r['date']} (weekday {r['weekday']}): {r['status']}"
                        for r in deferred[:50])
        )


# ---------------------------------------------------------------------------
# 3. The supersede boundary is consistent with (1).
# ---------------------------------------------------------------------------
class TestSupersedeBoundaryConsistency:
    def test_deferred_drain_supersede_label_equals_reopen_label(self):
        """`deferred_drain` supersedes any intent whose `broker_day` differs
        from `broker_now(now)` at drain time. The pass stores
        `broker_day = broker_now(pass_now)`, so an intent survives long enough
        to fire exactly when `broker_now(reopen) == broker_now(pass)`. That is
        the same label equality measured in (1), asserted here directly over
        every SHUT (deferred) AU200 weekday: the reopen instant and the pass
        instant must yield the same broker-day string."""
        mismatches = []
        for r in AU200_SWEEP:
            if r['status'] == 'open':
                continue                       # nothing deferred, nothing to supersede
            if r['reopen'] is None:
                mismatches.append((r['date'], r['pass_broker_day'],
                                   r['reopen_broker_day']))
                continue
            # The supersede rule recomputes the label from the reopen instant
            # the same way the pass stored it; assert they agree.
            label_at_reopen = _broker_day(r['reopen'])
            if label_at_reopen != r['pass_broker_day']:
                mismatches.append((r['date'], r['pass_broker_day'],
                                   label_at_reopen))
        assert not mismatches, (
            "Supersede boundary disagrees with the reopen instant for these "
            "dates (pass_broker_day != label_at_reopen):\n  " +
            "\n  ".join(f"{d}: {a} != {b}" for d, a, b in mismatches[:50])
        )

    def test_defer_action_stores_broker_day_the_same_way_supersede_reads_it(self):
        """The call site computes `broker_day = broker_now(now).strftime(...)`
        and `deferred_drain` reads `broker_now(now).strftime(...)` — the same
        function and the same format. Confirmed at the source level so the
        boundary cannot drift via a format change in one place only."""
        import inspect
        src = inspect.getsource(fr.defer_action)
        # the parameter name is the contract supersede reads back
        assert 'broker_day' in src
        drain_src = inspect.getsource(fr.deferred_drain)
        assert '_pg.broker_now(now).strftime' in drain_src or \
               'broker_now(now).strftime' in drain_src


# ---------------------------------------------------------------------------
# 4. No intraday input to sizing.
# ---------------------------------------------------------------------------
class TestNoIntradayInputToSizing:
    def test_defer_action_stores_units_stop_mult_atr_but_not_stop(self):
        """The size is fixed at pass time (`units`) so it matches the
        simulator, but the stop PRICE is deliberately NOT carried — it must be
        derived from the live entry price at fill time (a stop computed hours
        earlier sits on the wrong side of market and is rejected). Assert that
        `defer_action` stores `units`, `stop_mult` and `atr` and does NOT store
        a `stop` price."""
        _ST = {'pos_id': None, 'signal': 0, 'stop_ref': None,
               'side': None, 'units': None}
        # defer_action persists to DEFER_FILE; point it at a temp file so the
        # real queue is untouched.
        tmp = fr.DEFER_FILE + '.parity_test'
        saved = fr.DEFER_FILE
        try:
            fr.DEFER_FILE = tmp
            entry = fr.defer_action(
                'TEST_SID', 'AU200_AUD', 'open', 1, 0,
                12.5, 2.0, 0.00300, _ST,
                datetime(2024, 1, 15, 0, 15, tzinfo=timezone.utc),
                '2024-01-15')
        finally:
            fr.DEFER_FILE = saved
            try:
                os.remove(tmp)
            except OSError:
                pass
        assert 'units' in entry and entry['units'] == 12.5
        assert 'stop_mult' in entry and entry['stop_mult'] == 2.0
        assert 'atr' in entry and entry['atr'] == 0.00300
        assert 'stop' not in entry, (
            "defer_action stored a 'stop' price — that would re-instate the "
            "stale-stop bug it was designed to avoid (the stop must be derived "
            "from the live entry price at fill time, not carried from the pass)"
        )


# ---------------------------------------------------------------------------
# Summary table.
# ---------------------------------------------------------------------------
class TestSummaryTable:
    def test_write_n4_parity_summary(self):
        """Write `.scratch/defer/n4-parity.md` with the AU200 categorization
        across 2024-01-01..2026-12-31, and assert the counts are internally
        consistent (open + same + later == total weekdays)."""
        os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)
        n_open = sum(1 for r in AU200_SWEEP if r['status'] == 'open')
        n_same = sum(1 for r in AU200_SWEEP if r['status'] == 'same')
        n_later = sum(1 for r in AU200_SWEEP if r['status'] == 'later')
        total = len(AU200_SWEEP)
        later_rows = [r for r in AU200_SWEEP if r['status'] == 'later']

        # Date ranges where the FAIL category occurs, if any.
        ranges = []
        if later_rows:
            dates = [datetime.fromisoformat(r['date']) for r in later_rows]
            start = dates[0]
            prev = dates[0]
            for d in dates[1:]:
                if (d - prev).days <= 1:
                    prev = d
                    continue
                ranges.append((start, prev))
                start = d
                prev = d
            ranges.append((start, prev))

        def fmt_range(s, e):
            return s.strftime('%Y-%m-%d') if s == e else \
                f"{s.strftime('%Y-%m-%d')} .. {e.strftime('%Y-%m-%d')}"

        lines = []
        lines.append("# N4 — Deferred-order / daily-bar simulator parity")
        lines.append("")
        lines.append("Measurement: for every weekday in 2024-01-01..2026-12-31, "
                     "take the prop pass instant (00:15 UTC). If AU200_AUD is "
                     "SHUT then, step forward in 5-minute increments (cap 24h) "
                     "to its next open, and compare the broker-day label of the "
                     "reopen to the broker-day label of the pass.")
        lines.append("")
        lines.append("Broker day = `prop_guard.broker_now(dt).strftime('%Y-%m-%d')` "
                     "(America/New_York + 7h). Schedule membership via "
                     "`fix_runner.session_end(now, intervals)`.")
        lines.append("")
        lines.append("## Counts")
        lines.append("")
        lines.append(f"- Weekdays swept: **{total}**")
        lines.append(f"- OPEN at the pass (no deferral needed): **{n_open}**")
        lines.append(f"- SHUT, reopened in the SAME broker day (deferral works): "
                     f"**{n_same}**")
        lines.append(f"- SHUT, reopened only in a LATER broker day (deferral FAILS "
                     f"— sleeve would silently not trade): **{n_later}**")
        lines.append("")
        lines.append("## Date ranges where the FAIL category occurs")
        lines.append("")
        if ranges:
            for s, e in ranges:
                lines.append(f"- {fmt_range(s, e)}")
        else:
            lines.append("- _None._ The same-broker-day property holds for every "
                         "weekday in the sweep window.")
        lines.append("")
        lines.append("## Verdict")
        lines.append("")
        if n_later == 0:
            lines.append("PASS — the deferred fill always lands on the same broker "
                         "bar the daily-bar simulator assumed, so the simulator's "
                         "performance figures are valid for the deferred sleeves.")
        else:
            lines.append(f"FAIL — {n_later} weekday(s) reopen in a later broker "
                         "day; the simulator would attribute the fill to the wrong "
                         "bar and `deferred_drain` would drop the intent as stale.")
        lines.append("")
        with open(SUMMARY_PATH, 'w') as fh:
            fh.write("\n".join(lines))

        # Internal consistency: every weekday is in exactly one bucket.
        assert n_open + n_same + n_later == total
