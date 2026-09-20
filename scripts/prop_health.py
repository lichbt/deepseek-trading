#!/usr/bin/env python3
"""4-hourly health check for the Zeabur PROP pod, reported through Telegram.

WHY THIS EXISTS. `scripts/book_watch.py` already runs every 4 hours and watches
the book and the drawdown breaker, but it is deliberately ALERT-ONLY: it says
nothing when everything is fine, and it never asks whether the pod itself is
alive. That leaves two blind spots, both of which have already cost real money
somewhere in this project's history:

  * A pod that is up but NOT TRADING looks identical to a healthy one from the
    outside. The runner is `RUNNER_MODE=cron`: it places no orders on boot and
    waits on /data/trade_now, so a green pod proves nothing. The durable evidence
    is the `last_pass.json` receipt (fix_runner._write_receipt) and its AGE —
    "the pass never ran" and "it ran and blew up" are indistinguishable without
    it because pod logs retain only ~3h.
  * A health checker that silently stops running is indistinguishable from a
    healthy system. So this does not stay quiet: it messages EVERY run, with the
    all-clear when there is one. Silence from this job is itself a signal.

WHAT IT CHECKS
  1. pod     — deployment ready and the pod in phase Running (restarts surfaced)
  2. runner  — exactly one fix_runner process (two would double the book blind)
  3. pass    — last_pass.json age/ok, and no unconsumed trade_now trigger
  4. guard   — the drawdown breaker ARMED and still sampling
  5. ctrader (exec) — the broker has not rejected the pod's credentials
  6. oanda (data)   — the pod's PRICE source answers, from inside the pod
  7. limits  — daily/total drawdown against the prop limits the breaker fires on

EXECUTION AND DATA ARE DIFFERENT DEPENDENCIES. Only execution is cTrader; every
signal, and the live prices behind the software stop check, come from OANDA
(fix_runner.py:30). A correct cTrader auth with a dead OANDA feed still means no
entries and blind stops, so the two are checked and reported separately.

The guard and broker-auth JUDGEMENT is imported from book_watch rather than
re-derived: its thresholds (guard_stale_seconds, _AUTH_REJECT_MARKERS) are the
product of three separate silent failures and must not be allowed to drift.

    ./venv/bin/python scripts/prop_health.py             # check + Telegram
    ./venv/bin/python scripts/prop_health.py --dry-run   # print only, send nothing
"""
import argparse
import base64
import html
import json
import os
import subprocess
import sys
from collections import namedtuple
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import book_watch                      # reused judgement: guard + broker auth
import env_loader

env_loader.load_env()                  # launchd has no shell; .env must be read here

INTERLOCK = os.path.join(HERE, 'zeabur_interlock.sh')

OK, WARN, FAIL, UNKNOWN = 'OK', 'WARN', 'FAIL', 'UNKNOWN'
Check = namedtuple('Check', 'name status detail')

# The host cron fires the pass in the 00:00 UTC hour (zeabur_interlock cron-install),
# so a healthy receipt ages just under 24h between passes. 26h allows for a late
# trigger; past 30h an entire daily pass has been missed.
PASS_WARN_HOURS = 26.0
PASS_FAIL_HOURS = 30.0
# The wait loop polls every TRIGGER_POLL (60s), so a trigger still sitting on disk
# after ten minutes means the loop is wedged or the process is dead.
TRIGGER_STUCK_MINUTES = 10.0
# A receipt cannot exist before the pod has been up long enough to be asked for a
# pass; below this an absent receipt is "too new to judge", not a failure.
PASS_GRACE_HOURS = 2.0


# ---------------------------------------------------------------------------
# parsing the health-probe line protocol
# ---------------------------------------------------------------------------

def _b64json(value):
    if not value or value == 'NONE':
        return None
    try:
        return json.loads(base64.b64decode(value).decode('utf-8', 'replace'))
    except Exception:
        return None


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_probe(text):
    """KEY=value lines -> a snapshot dict. ENV_* collapses under 'env'; repeated
    GUARD_FILE/GUARD_B64 pairs collect into 'guards' keyed by filename."""
    snap = {'env': {}, 'guards': {}}
    pending = None
    for raw in (text or '').replace('\r', '').splitlines():
        if '=' not in raw:
            continue
        key, _, value = raw.partition('=')
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if key.startswith('ENV_'):
            snap['env'][key[4:]] = value
        elif key == 'GUARD_FILE':
            pending = value
        elif key == 'GUARD_B64':
            if pending:
                snap['guards'][pending] = _b64json(value)
            pending = None
        else:
            snap[key] = value
    return snap


def parse_pods(value):
    """'name|phase|restarts|startTime;...' -> [dict]. One pod per ';' chunk."""
    pods = []
    for chunk in (value or '').split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split('|')
        if len(parts) < 4:
            continue
        pods.append({'name': parts[0], 'phase': parts[1],
                     'restarts': _int(parts[2], 0) or 0, 'started': parts[3]})
    return pods


def _venue(snap):
    env = snap.get('env') or {}
    return (env.get('PROP_GUARD_VENUE') or env.get('VENUE') or '').strip().lower()


def pick_guard_state(snap):
    """The guard names its state file by venue, so pick the one this pod writes.

    A venue change leaves the OLD file on the volume; reading the wrong one would
    report another venue's drawdown as this book's."""
    guards = {k: v for k, v in (snap.get('guards') or {}).items() if isinstance(v, dict)}
    if not guards:
        return None
    venue = _venue(snap)
    want = ('prop_guard_state.json' if not venue or venue == 'oanda'
            else f'prop_guard_state_{venue}.json')
    return guards.get(want) or guards[sorted(guards)[-1]]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_pod(snap, now):
    desired = _int(snap.get('DEPLOY_DESIRED'))
    ready = _int(snap.get('DEPLOY_READY'), 0) or 0
    pods = parse_pods(snap.get('POD_JSON'))
    running = [p for p in pods if p['phase'] == 'Running']
    if desired is None:
        return [Check('pod', UNKNOWN, 'the deployment did not report spec.replicas')]
    if desired == 0:
        # NOT a hard failure: `zeabur_interlock.sh on` deliberately scales to 0
        # before a push. But if it stays 0 the prop book is simply not trading, so
        # it must never read as OK.
        return [Check('pod', WARN,
                      'deployment is scaled to 0 — expected ONLY inside an '
                      'interlock/deploy (zeabur_interlock.sh on/off). While it '
                      'stays 0 the prop book is NOT trading.')]
    if ready < 1 or not running:
        seen = ', '.join(f"{p['name'].split('-')[-1]}:{p['phase']}" for p in pods) or 'no pods'
        return [Check('pod', FAIL,
                      f'DESIRED={desired} READY={ready}, pods [{seen}] — the prop '
                      f'runner is NOT up')]
    restarts = max(p['restarts'] for p in pods)
    detail = f'Running {ready}/{desired}'
    started = _parse_dt(running[0]['started'])
    if started:
        up_h = (now - started).total_seconds() / 3600.0
        detail += f', up {up_h / 24:.1f}d' if up_h >= 24 else f', up {up_h:.1f}h'
    detail += f', restarts {restarts}'
    if restarts > 2:
        return [Check('pod', WARN, detail + ' — repeated restarts; read the pod log')]
    return [Check('pod', OK, detail)]


def check_runner(snap):
    count = _int(snap.get('FIX_RUNNER_PROCS'))
    if count is None:
        return [Check('runner', UNKNOWN, 'could not read the fix_runner process count')]
    if count == 0:
        return [Check('runner', FAIL,
                      'NO fix_runner process is running — nothing can consume a '
                      'trade trigger or enforce a stop')]
    if count > 1:
        return [Check('runner', FAIL,
                      f'{count} fix_runner processes — exactly ONE may run per '
                      f'broker account; two trade it blind to each other and '
                      f'double the book')]
    return [Check('runner', OK, '1 fix_runner process')]


def _pod_uptime_hours(snap, now):
    pods = parse_pods(snap.get('POD_JSON'))
    for pod in pods:
        started = _parse_dt(pod.get('started'))
        if started:
            return (now - started).total_seconds() / 3600.0
    return None


def check_pass(snap, now):
    checks = []
    present = str(snap.get('TRADE_NOW_PRESENT', '')).strip() == '1'
    if present:
        mtime = _int(snap.get('TRADE_NOW_MTIME'), 0) or 0
        age_min = (now.timestamp() - mtime) / 60.0 if mtime else None
        if age_min is not None and age_min > TRIGGER_STUCK_MINUTES:
            checks.append(Check('trigger', FAIL,
                                f'a trade_now trigger has sat unconsumed for '
                                f'{age_min:.0f} min (the loop polls every 60s) — '
                                f'the runner is wedged or dead'))
        else:
            checks.append(Check('trigger', WARN,
                                'a trade trigger is pending and young — a pass is '
                                'in flight right now'))

    mode = (snap.get('env', {}).get('RUNNER_MODE') or '').strip().lower()
    if mode != 'cron':
        checks.append(Check('pass', UNKNOWN,
                            f"RUNNER_MODE={mode or 'unset'} — last_pass.json is "
                            f"written only by the cron-triggered path, so its age "
                            f"is not a heartbeat on this pod"))
        return checks

    receipt = _b64json(snap.get('LAST_PASS_B64'))
    if not receipt:
        up_h = _pod_uptime_hours(snap, now)
        if up_h is not None and up_h < PASS_GRACE_HOURS:
            checks.append(Check('pass', UNKNOWN,
                                f'no pass receipt yet, but the pod is only '
                                f'{up_h:.1f}h old'))
        else:
            checks.append(Check('pass', FAIL,
                                'RUNNER_MODE=cron but there is NO pass receipt on '
                                'the volume — no trigger has ever been consumed. '
                                'Check that the host cron is installed '
                                '(zeabur_interlock.sh cron-show)'))
        return checks

    if not receipt.get('ok', False):
        checks.append(Check('pass', FAIL,
                            f"the last pass FAILED: {receipt.get('error')!r} "
                            f"(finished {receipt.get('finished')})"))
    finished = _parse_dt(receipt.get('finished'))
    if finished is None:
        checks.append(Check('pass', UNKNOWN,
                            f"unparseable receipt finished={receipt.get('finished')!r}"))
        return checks
    age_h = (now - finished).total_seconds() / 3600.0
    if age_h > PASS_FAIL_HOURS:
        checks.append(Check('pass', FAIL,
                            f'last pass finished {age_h:.1f}h ago '
                            f'(>{PASS_FAIL_HOURS:.0f}h) — a daily pass has been missed'))
    elif age_h > PASS_WARN_HOURS:
        checks.append(Check('pass', WARN,
                            f'last pass finished {age_h:.1f}h ago '
                            f'(>{PASS_WARN_HOURS:.0f}h) — later than the daily '
                            f'trigger should leave it'))
    elif receipt.get('ok', False):
        checks.append(Check('pass', OK, f'last pass finished {age_h:.1f}h ago and reported ok'))
    return checks


def check_guard(snap, now):
    """Reuses book_watch's threshold and verdict, so the two cannot disagree."""
    env = snap.get('env', {})
    raw = env.get('PROP_GUARD_HALT')
    armed = None if raw in (None, 'UNSET', '') else raw.strip() == '1'
    every = _int(env.get('PROP_GUARD_EVERY'))
    guard = pick_guard_state(snap)
    last = guard.get('last_updated') if guard else None
    if armed is None and last is None:
        return [Check('guard', UNKNOWN,
                      'the pod reported neither PROP_GUARD_HALT nor a guard state file')]
    max_age = book_watch.guard_stale_seconds(every)
    findings = book_watch.guard_findings(armed, last, None, now, max_age)
    if findings:
        return [Check('guard', FAIL, detail) for _, _, _, detail in findings]
    return [Check('guard', OK,
                  f'ARMED, last sampled {last} (stale threshold {max_age / 60:.0f} min)')]


def probe_auth(script=INTERLOCK, timeout=90):
    """-> (log_read, reason). Unlike book_watch.probe_broker_auth this separates
    'read the log, no rejection' from 'could not read the log at all', because a
    heartbeat that reports a clean check it never ran is worse than no heartbeat.

    The marker set is imported, not copied: it encodes two real incidents."""
    if not os.path.exists(script):
        return False, None
    try:
        p = subprocess.run(['bash', script, 'logs'], cwd=ROOT, timeout=timeout,
                           capture_output=True, text=True)
    except Exception:
        return False, None
    log = (p.stdout or '').replace('\r', '')
    if not log.strip():
        return False, None
    for line in reversed(log.splitlines()):
        if any(m in line.lower() for m in book_watch._AUTH_REJECT_MARKERS):
            return True, line.strip()[:200]
    return True, None


def check_auth(now, script=INTERLOCK, timeout=90):
    seen, reason = probe_auth(script, timeout)
    if not seen:
        return [Check('ctrader (exec)', UNKNOWN,
                      'the pod log could not be read — broker auth is UNVERIFIED, '
                      'which is not the same as clean')]
    findings = book_watch.broker_auth_findings(reason, now)
    if findings:
        return [Check('ctrader (exec)', FAIL, findings[0][3])]
    return [Check('ctrader (exec)', OK, 'no broker rejection in the pod log')]


def check_oanda(snap):
    """OANDA is the pod's DATA source, and it is a different dependency from the
    cTrader execution venue: correct broker auth plus a dead OANDA feed still
    means no signals and no live prices for the software stop check.

    The probe runs INSIDE the pod (health-probe exec), so it tests the pod's own
    egress and its own token. The last-bar age is reported but never failed on:
    FX is shut at weekends, where an old newest bar is correct rather than a fault.
    """
    status = (snap.get('OANDA_STATUS') or '').strip()
    last = snap.get('OANDA_LAST')
    err = snap.get('OANDA_ERROR')
    if status == 'OK':
        return [Check('oanda (data)', OK,
                      'reachable' + (f', last H1 bar {last}' if last else ''))]
    if status == 'EMPTY':
        return [Check('oanda (data)', WARN,
                      'OANDA answered 200 but returned no candles — check the '
                      'token and the instrument')]
    if status == 'NOT_CONFIGURED':
        return [Check('oanda (data)', FAIL,
                      'OANDA_API_TOKEN is not set in the pod — the runner has no '
                      'price data at all')]
    if status == 'UNREACHABLE':
        return [Check('oanda (data)', UNKNOWN,
                      'could not exec into the pod to test OANDA')]
    if status == 'FAIL':
        return [Check('oanda (data)', FAIL,
                      f'OANDA refused or was unreachable from the pod: {err} — '
                      f'signals cannot be evaluated and the live-price stop '
                      f'checks go blind on a cache miss')]
    return [Check('oanda (data)', UNKNOWN, 'no OANDA probe result came back')]


def _limit_check(name, used, limit, fraction, what):
    if limit is None or limit <= 0:
        return Check(name, UNKNOWN, 'the pod did not report this limit')
    if used is None:
        return Check(name, UNKNOWN, 'the guard state does not carry this drawdown')
    if used >= limit:
        return Check(name, FAIL,
                     f'{what} {used:.2%} has reached the {limit:.1%} limit — the '
                     f'breaker flattens here')
    if used >= limit * fraction:
        return Check(name, WARN,
                     f'{what} {used:.2%} is past {fraction:.0%} of the {limit:.1%} '
                     f'limit (the breaker halts at {limit * fraction:.2%})')
    return Check(name, OK, f'{what} {used:.2%} of the {limit:.1%} limit')


def check_limits(snap):
    guard = pick_guard_state(snap)
    if not guard:
        return [Check('limits', UNKNOWN,
                      'no guard state on the volume — drawdown is unreadable')]
    env = snap.get('env', {})
    daily_limit = _float(env.get('PROP_DAILY_DD_LIMIT'), 0.03)
    total_limit = _float(env.get('PROP_TOTAL_DD_LIMIT'), 0.10)
    fraction = _float(env.get('PROP_HALT_FRACTION'), 0.80)

    anchor = _float(guard.get('day_anchor_nav'))
    low = _float(guard.get('day_low_nav'))
    daily_used = abs((low - anchor) / anchor) if anchor and low else None

    max_total = _float(guard.get('max_total_dd'))
    checks = [_limit_check('daily DD', daily_used, daily_limit, fraction,
                           "today's intraday move"),
              _limit_check('total DD', abs(max_total) if max_total is not None else None,
                           total_limit, fraction, 'worst peak-to-trough')]

    nav = _float(guard.get('last_nav'))
    start = _float(guard.get('start_nav'))
    if nav is not None:
        detail = f'NAV {nav:,.2f}'
        if start:
            detail += f' vs start {start:,.2f} ({(nav - start) / start:+.2%})'
        peak = _float(guard.get('peak_nav'))
        if peak:
            detail += f', peak {peak:,.2f}'
        checks.append(Check('nav', OK, detail))
    return checks


# ---------------------------------------------------------------------------
# assembly, rendering, delivery
# ---------------------------------------------------------------------------

def unknown_checks(reason):
    return [Check('pod', UNKNOWN, reason),
            Check('runner', UNKNOWN, 'not read'),
            Check('pass', UNKNOWN, 'not read'),
            Check('guard', UNKNOWN, 'not read'),
            Check('oanda (data)', UNKNOWN, 'not read'),
            Check('limits', UNKNOWN, 'not read')]


ICONS = {OK: '✅', WARN: '⚠️', FAIL: '❌', UNKNOWN: '❓'}


def render(checks, now):
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status in (WARN, UNKNOWN)]
    if fails:
        header = f'🚨 <b>Prop Zeabur health — {len(fails)} FAILED</b>'
    elif warns:
        header = f'⚠️ <b>Prop Zeabur health — {len(warns)} to look at</b>'
    else:
        header = '🩺 <b>Prop Zeabur health — OK</b>'
    lines = [header, now.strftime('%Y-%m-%d %H:%M UTC'), '']
    for c in checks:
        lines.append(f'{ICONS[c.status]} <b>{c.name}</b>: '
                     f'{html.escape(str(c.detail), quote=False)}')
    if fails:
        lines += ['', 'The prop book trades real money. Go and look.']
    return '\n'.join(lines)


def run_probe(script=INTERLOCK, timeout=180):
    """-> (snapshot, error). error is set only when no snapshot came back at all."""
    if not os.path.exists(script):
        return None, f'no interlock script at {script} — not the ops machine?'
    try:
        p = subprocess.run(['bash', script, 'health-probe'], cwd=ROOT,
                           timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return None, f'the pod probe timed out after {timeout}s'
    except Exception as exc:
        return None, f'the pod probe failed: {exc!r}'
    out = (p.stdout or '').replace('\r', '')
    if 'HEALTH_NOW=' not in out:
        why = _first_error_line(p)
        return None, ('could not read the pod — no health snapshot came back'
                      + (f' ({why})' if why else ''))
    return parse_probe(out), None


def _first_error_line(proc):
    """Pull one human-readable reason out of an expect/ssh failure.

    expect prints its Tcl traceback after the message, so the LAST line is a
    fragment of the spawn command ('    -o LogLevel=ERROR ...') and the useful
    sentence is the FIRST one."""
    for stream in (proc.stderr or '', proc.stdout or ''):
        for line in stream.replace('\r', '').splitlines():
            line = line.strip()
            if not line or line.startswith(('"', 'while ', 'invoked from')):
                continue
            return line[:160]
    return None


def collect_checks(snap, now, script=INTERLOCK, auth_timeout=90):
    checks = []
    checks += check_pod(snap, now)
    checks += check_runner(snap)
    checks += check_pass(snap, now)
    checks += check_guard(snap, now)
    checks += check_auth(now, script, auth_timeout)
    checks += check_oanda(snap)
    checks += check_limits(snap)
    return checks


def send(text):
    """The existing Telegram notification path (telegram_bot.notify_html)."""
    try:
        from telegram_bot import notify_html
    except Exception as exc:
        print(f'WARNING: telegram_bot unavailable: {exc}', file=sys.stderr)
        return False
    try:
        return bool(notify_html(text))
    except Exception as exc:
        print(f'WARNING: telegram send failed: {exc}', file=sys.stderr)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='print the report, send nothing')
    ap.add_argument('--timeout', type=int, default=180,
                    help='seconds to wait for the pod probe (default 180)')
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    try:
        snap, error = run_probe(timeout=args.timeout)
    except KeyboardInterrupt:
        return 130
    checks = unknown_checks(error) if error else collect_checks(snap, now)

    report = render(checks, now)
    print(report)

    if args.dry_run:
        print('\n(dry run — nothing sent)')
    else:
        sent = send(report)
        print('\n[telegram sent]' if sent else
              '\n[telegram NOT sent — check token/chat env]')

    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == '__main__':
    sys.exit(main())
