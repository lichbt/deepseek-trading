#!/usr/bin/env python3
"""
prop_guard.py — account-level drawdown monitor for prop-firm rules.

Tracks the OANDA account NAV and measures it against two hard limits a typical
prop-firm account imposes:

    DAILY_DD_LIMIT  — max loss within one trading day   (default 5%)
    TOTAL_DD_LIMIT  — max trailing drawdown from peak    (default 10%)

Both are measured on OPEN / intraday equity (NAV = balance + unrealized P&L),
which is the strict interpretation. Total drawdown is trailing-from-peak (the
strict variant); switch PEAK_ANCHOR to 'start' for a static-from-start rule.

Run frequently (e.g. every 5 min via launchd) so the intraday low is sampled
finely — the daily limit is about the worst intraday dip, not the close.

Usage:
    python prop_guard.py            # update state, print status, alert if near limit
    python prop_guard.py --quiet    # update state silently (for schedulers)

State is persisted to prop_guard_state.json next to this file.
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover - py<3.9
    ZoneInfo = None

OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN  = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL   = 'https://api-fxpractice.oanda.com'

# WHICH ACCOUNT THIS GUARD WATCHES. Default 'oanda' keeps existing behaviour for
# live_test and hourly_report, which both import this module for the paper book.
#
# This file was named for the prop account but has only ever read the OANDA one —
# so the account with real money on it has never been monitored. PROP_GUARD_VENUE
# =ctrader points it at The5ers via the Open API.
VENUE = os.getenv('PROP_GUARD_VENUE', 'oanda').strip().lower()

# One state file PER VENUE. The two accounts have different balances, different
# peaks and different day anchors; sharing a file would splice one account's
# history onto the other and silently corrupt every drawdown figure in it.
_STATE_NAME = ('prop_guard_state.json' if VENUE == 'oanda'
               else f'prop_guard_state_{VENUE}.json')


def _resolve_state_dir(here=None):
    """Where the anchors live. On the pod that MUST be the mounted volume.

    `here` is injectable so a test can point at a fake layout. Monkeypatching
    os.path.abspath instead does not work: realpath() calls it internally, so the
    patch corrupts the symlink resolution this function is built on.

    This file used to write beside the module, i.e. /app in the container — which
    is not the volume, and is in .dockerignore, so the file never shipped and was
    re-created empty on EVERY pod start. start_nav then re-seeded from whatever
    equity happened to be current, and the total limit (defined as static from the
    INITIAL balance) silently re-based itself downward: restart at 95k on a 100k
    account and the 80%-of-10% halt moves from 92,000 to 87,400 — below the 90,000
    DQ line, so the account is gone before the breaker fires. The daily anchor
    fails the same way across an intraday restart.

    fix_runner already solved this: /app/fix_runner_state.json is a symlink to the
    volume, so realpath'ing it finds /data with no new env var to forget — which
    matters, because PROP_GUARD_VENUE going unset on the pod is precisely what a
    forgotten env var looks like. Falls back to the module directory when there is
    no symlink (every local/dev run), so behaviour off the pod is unchanged.
    """
    override = os.getenv('PROP_GUARD_STATE_DIR', '').strip()
    if override:
        return override
    if here is None:
        here = os.path.dirname(os.path.abspath(__file__))
    runner_state = os.path.join(here, 'fix_runner_state.json')
    if os.path.islink(runner_state):
        volume = os.path.dirname(os.path.realpath(runner_state))
        if os.path.isdir(volume):
            return volume
    return here


STATE_DIR = _resolve_state_dir()
STATE_FILE = os.path.join(STATE_DIR, _STATE_NAME)
HALT_FLAG_FILE = os.path.join(STATE_DIR, 'trading_halt.flag')

# The contractual starting balance the STATIC total limit is measured from.
#
# Its own variable on purpose. FIX_START_EQUITY looks like the right thing and is
# already in the pod env, but it is the FIX-era SIZING figure and still reads 2500
# — the old ~$2.5k account. Anchoring a 100k account's 10% limit to 2500 would put
# it 97% "down" and halt the book on the first tick. A wrong anchor is not a
# degraded guard, it is an inverted one, so this never falls back to that name.
#
# Unset (the default) keeps the old behaviour: seed from the first NAV observed.
_start_env = os.getenv('PROP_START_BALANCE', '').strip()
START_BALANCE = float(_start_env) if _start_env else None

# Kill switch (opt-in). When PROP_GUARD_HALT=1, write trading_halt.flag once a
# drawdown reaches HALT_FRACTION of its limit — SOFTER than the alert WARN so
# trading stops with margin before the hard limit. live_test reads the flag and
# blocks new risk / flattens. Default OFF: absent the env, prop_guard only
# alerts (no behaviour change). PROP_HALT_FLATTEN=1 (default) closes open
# positions on halt (needed to protect the DAILY limit, since open losses
# count); =0 blocks new entries only.
HALT_ENABLED   = os.getenv('PROP_GUARD_HALT', '0') == '1'
# cTrader wire volume is centi-units (ctrader_exec.UNITS_TO_VOLUME). Mirrored rather
# than imported so the oanda path never pulls in the execution stack.
CT_UNITS_TO_VOLUME = 100.0
HALT_FLATTEN   = os.getenv('PROP_HALT_FLATTEN', '1') == '1'
HALT_FRACTION  = float(os.getenv('PROP_HALT_FRACTION', '0.80'))  # halt at 80% of a limit

# --- Prop-firm limits (fractions of the relevant anchor) ---
# Configured for FTMO 2-Step and The5ers High Stakes, which share the same
# risk structure: 5% daily loss + 10% STATIC max loss from the initial balance
# (neither trails up from peak), with a 10% Phase-1 / 5% Phase-2 profit target.
# Both measure intraday on equity (open positions count toward the limits).
#
# CHANGED 2026-08-05 5% -> 3%: the account being bought is the The5ers 100k
# TWO-STEP, whose daily limit is 3%. At 5% the 80% halt fired at -4.0%, i.e.
# a full percentage point PAST a DQ — the guard would have alerted only after
# the account was already gone. Env-overridable so one file serves both products.
DAILY_DD_LIMIT = float(os.getenv('PROP_DAILY_DD_LIMIT', '0.03'))
TOTAL_DD_LIMIT = float(os.getenv('PROP_TOTAL_DD_LIMIT', '0.10'))  # STATIC, from starting balance
PROFIT_TARGET  = 0.10    # Phase-1 profit target (Phase 2 is 5%); informational
WARN_FRACTION  = 0.70    # alert once a drawdown reaches this fraction of its limit
PEAK_ANCHOR    = 'start'  # 'start' = static-from-initial (FTMO 2-step / The5ers);
                          # 'peak'  = trailing-from-peak (FTMO 1-step, futures-style)

# Daily reset boundary (UTC hour). FTMO resets at 00:00 CE(S)T (= 22:00 UTC in
# summer / 23:00 UTC in winter); The5ers resets at 00:00 broker-server time.
# 22 ≈ 00:00 CEST (current season). Adjust for winter / your broker's server TZ.
# The trading day rolls at 00:00 on the BROKER's clock — not at a fixed UTC hour.
#
# VERIFIED 2026-08-05 against the venue's own D1 trendbars on ctid 48171893: bars
# open at 21:00 UTC, i.e. 00:00 at UTC+3. The previous constant was 22, which is
# the SAME boundary expressed in winter (00:00 at UTC+2 = 22:00 UTC) — so it was
# set once and never followed DST, and was an hour late for half of every year.
#
# CORRECTED 2026-08-06. That verification was run in August, when EU-DST, US-DST
# and a fixed UTC+3 all give the same answer, so it could not identify WHICH +3
# the broker keeps — and 'Europe/Athens' guessed the EU one. Re-measured across
# three seasons of D1 trendbars (EUR_USD, ctid 48171893):
#
#     2026-01-05..31  bars open 22:00 UTC  -> UTC+2
#     2026-03-09..27  bars open 21:00 UTC  -> UTC+3   <- the discriminator
#     2026-07-06..31  bars open 21:00 UTC  -> UTC+3
#
# The broker switched on 2026-03-08, the US date, three weeks before Europe
# (2026-03-29). So the server clock is America/New_York + 7h — the standard MT5
# convention that keeps the daily bar closing at 17:00 New York. Europe/Athens
# tracks the EU dates instead and therefore disagrees for about four weeks a
# year (Mar 8-29 and Oct 25 - Nov 1 in 2026), rolling the day an HOUR LATE in
# exactly the window where FX reopens.
#
# An hour of skew is not cosmetic here: the anchor decides which equity the daily
# loss is measured FROM, so a late roll carries the previous session's loss into
# the new day and can report a breach that did not happen (or hide one that did).
#
# Expressed as an offset FROM a zone rather than as a zone because no Olson zone
# is "UTC+2/+3 on the US DST calendar" — the thing the broker actually uses.
BROKER_CLOCK_TZ  = os.getenv('PROP_BROKER_CLOCK_TZ', 'America/New_York')
BROKER_CLOCK_OFF = timedelta(hours=float(os.getenv('PROP_BROKER_CLOCK_OFFSET_H', '7')))

# Escape hatch for a broker that really does keep a named zone (FTMO resets at
# 00:00 CE(S)T -> PROP_DAY_RESET_TZ=Europe/Berlin). Empty = use the offset above.
DAY_RESET_TZ = os.getenv('PROP_DAY_RESET_TZ', '').strip()

# Fail loudly rather than silently anchoring the daily limit to the wrong clock:
# a guard measuring from the wrong equity is worse than one that does not start.
if ZoneInfo is None:
    raise RuntimeError('prop_guard needs zoneinfo (Python 3.9+) to place the day boundary')
try:
    _DAY_TZ = ZoneInfo(DAY_RESET_TZ) if DAY_RESET_TZ else None
    _CLOCK_TZ = ZoneInfo(BROKER_CLOCK_TZ)
except Exception as _exc:                              # missing tzdata, bad name
    raise RuntimeError(
        f'prop_guard cannot load timezone {DAY_RESET_TZ or BROKER_CLOCK_TZ!r} ({_exc}). '
        'Install tzdata or set PROP_DAY_RESET_TZ / PROP_BROKER_CLOCK_TZ.')


def _fetch_nav_oanda():
    """Return current OANDA account NAV (balance + unrealized P&L), or None on failure."""
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        return None
    try:
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
        r = requests.get(url, headers={'Authorization': f'Bearer {OANDA_API_TOKEN}'}, timeout=8)
        r.raise_for_status()
        acct = r.json()['account']
        return float(acct.get('NAV', acct.get('balance', 0.0)))
    except Exception as e:
        print(f'[prop_guard] NAV fetch failed: {e}', file=sys.stderr)
        return None


def _fetch_account_ctrader():
    """(balance, equity) for the cTrader account, or (None, None) on failure.

    The Open API reports no equity field — ProtoOATrader carries `balance` only —
    so fix_runner's adapter uses bare balance as its 'equity' (fix_runner.py:697).
    Fine for sizing, wrong for a drawdown limit that counts floating loss.

    Equity comes from the BROKER's own valuation:

        equity = balance + SUM(netUnrealizedPnL)      [ProtoOAGetPositionUnrealizedPnL]

    `net` includes commission and swap. That matters: this used to be assembled
    here from bid/ask, exit-side and a quote->USD conversion, which valued the
    PRICE MOVE ONLY and therefore omitted swap — the book's largest unmodelled
    cost, accruing with holding time, against a limit that counts floating loss.
    The reconstruction also needed every symbol mapped and a live tick for each,
    so one unmapped symbol or one shut market blinded the whole guard.

    BOTH legs are returned because the daily limit needs them separately: The5ers
    snapshots max(balance, equity) at midnight server time, so a day that opens
    with a floating LOSS is measured from the higher BALANCE. Returning equity
    alone made that base unrecoverable.

    Returns (None, None) — never a partial figure — if any leg fails.
    """
    try:
        import ctrader_client
        client = ctrader_client.get_client().start()
        balance = float(client.get_trader()['balance'])
        pnl = client.get_unrealized_pnl()
        return balance, balance + sum(v['net'] for v in pnl.values())
    except Exception as e:
        print(f'[prop_guard] cTrader equity failed: {e}', file=sys.stderr)
        return None, None


def _fetch_nav_ctrader():
    """cTrader account equity only. Kept for callers that don't need the balance
    (fix_runner._guard_equity)."""
    return _fetch_account_ctrader()[1]


def _fetch_account():
    """(balance, equity) for the configured venue. OANDA reports NAV directly and
    its balance is a separate field; only the cTrader path has to assemble it."""
    if VENUE == 'ctrader':
        return _fetch_account_ctrader()
    nav = _fetch_nav_oanda()
    return _fetch_balance_oanda(), nav


def _fetch_balance_oanda():
    """OANDA account balance (realised only), or None."""
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        return None
    try:
        url = f'{OANDA_BASE_URL}/v3/accounts/{OANDA_ACCOUNT_ID}'
        r = requests.get(url, headers={'Authorization': f'Bearer {OANDA_API_TOKEN}'}, timeout=8)
        r.raise_for_status()
        return float(r.json()['account'].get('balance', 0.0))
    except Exception:
        return None


def _fetch_nav():
    """Current account equity for the configured venue, or None on failure."""
    return _fetch_nav_ctrader() if VENUE == 'ctrader' else _fetch_nav_oanda()


def daily_base(balance, equity):
    """The5ers' daily-loss base: max(balance, equity) at midnight server time.

    Quoted rule: "It compares your starting balance and starting equity, selects
    the higher number, and uses it to set your loss limit for the next 24 hours."

    Anchoring on equity alone (what this did before) agrees with the firm only when
    the day opens in floating PROFIT. Open it in floating LOSS and the firm
    measures from the HIGHER balance, so its floor sits ABOVE ours and the account
    can be disqualified while the guard still reads green — carry -$2,000 into the
    roll on a 100k and the firm's 3% floor is 97,000 while an equity anchor puts it
    at 95,060. The error is one-directional and it is the dangerous direction.

    Pure. `None` legs fall back to whichever is available.
    """
    if balance is None:
        return equity
    if equity is None:
        return balance
    return max(float(balance), float(equity))


def _trading_day(now: datetime) -> str:
    """YYYY-MM-DD label for the current trading day, on the broker's clock.

    Default path: the broker keeps America/New_York + 7h (00:00 server = 17:00 NY),
    so DST is handled by the NY zone and the offset rides along with it — correct
    across both US switchovers with no edit, and correct during the weeks when the
    US and EU calendars disagree. Setting PROP_DAY_RESET_TZ switches to a plain
    named zone for a broker that genuinely uses one.
    """
    if _DAY_TZ is not None:
        return now.astimezone(_DAY_TZ).strftime('%Y-%m-%d')
    # Add the offset to the LOCAL wall clock, not to UTC: that is what makes the
    # boundary follow the NY DST transition instead of drifting an hour past it.
    local = now.astimezone(_CLOCK_TZ).replace(tzinfo=None)
    return (local + BROKER_CLOCK_OFF).strftime('%Y-%m-%d')


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, 'w') as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    except Exception as e:
        print(f'[prop_guard] state save failed: {e}', file=sys.stderr)


def _sane_start_balance(nav: float) -> float:
    """PROP_START_BALANCE if it is credible against live NAV, else NAV.

    A configured anchor beats an observed one — that is the whole point — but a
    STALE one is worse than either, and this is the exact shape of the mistake
    waiting to be made: FIX_START_EQUITY still says 2500, so a copy-paste into
    PROP_START_BALANCE would put a 100k account 97% "down" and halt it on the
    first tick. A prop account cannot be 50% down and still open (the 10% DQ
    fires five times sooner), so that gap can only mean a misconfigured variable.
    Refuse it and fall back rather than act on a figure that cannot be true.
    """
    if START_BALANCE is None or START_BALANCE <= 0:
        return nav
    if abs(nav - START_BALANCE) / START_BALANCE > 0.5:
        print(f'[prop_guard] IGNORING PROP_START_BALANCE={START_BALANCE:g}: live NAV '
              f'{nav:,.2f} is {abs(nav-START_BALANCE)/START_BALANCE*100:.0f}% away — '
              f'that cannot be a live prop account. Anchoring on NAV instead.',
              file=sys.stderr)
        return nav
    return START_BALANCE


def _fetch_balance():
    """Realised balance for the configured venue, or None. Called only at the day
    roll (the base is a midnight snapshot), so it costs one request a day."""
    if VENUE != 'ctrader':
        return _fetch_balance_oanda()
    try:
        import ctrader_client
        return float(ctrader_client.get_client().start().get_trader()['balance'])
    except Exception as e:
        print(f'[prop_guard] balance fetch failed: {e}', file=sys.stderr)
        return None


def update(nav: float = None, balance: float = None) -> dict:
    """Fetch NAV (if not supplied), update the persisted DD state, and return a
    metrics dict. Safe to call frequently — it's the high-frequency sampler that
    keeps peak / daily-low accurate. Never raises into a caller.

    `balance` is only consulted when the trading day rolls, because the daily base
    is a midnight snapshot of max(balance, equity); pass it when the caller already
    has it, otherwise it is fetched at the roll only."""
    try:
        if nav is None:
            balance, nav = _fetch_account()
        if nav is None:
            return {}

        st = _load_state()
        now = datetime.now(timezone.utc)
        day = _trading_day(now)

        # First run — seed everything from current NAV, except the total-limit
        # anchor, which is contractual rather than observed when it is configured.
        seed = _sane_start_balance(nav)
        if not st:
            if balance is None:
                balance = _fetch_balance()
            st = {'peak_nav': max(nav, seed), 'start_nav': seed, 'day': day,
                  'day_anchor_nav': daily_base(balance, nav),
                  'day_base_balance': balance, 'day_base_equity': nav,
                  'day_low_nav': nav,
                  'max_total_dd': 0.0, 'worst_daily_dd': 0.0}
        elif START_BALANCE is not None and st.get('start_nav') != seed:
            # Self-heal state seeded before the balance was configured (or by a
            # restart on the old ephemeral path). Without this the bad anchor is
            # now DURABLE — persisting it to the volume would preserve the very
            # error this change removes.
            print(f'[prop_guard] start_nav {st.get("start_nav")} -> {seed} '
                  f'(PROP_START_BALANCE)', file=sys.stderr)
            st['start_nav'] = seed

        # New trading day — snapshot the daily base and reset the intraday low.
        # The base is max(balance, equity) at the roll, not equity: The5ers' risk
        # engine "compares your starting balance and starting equity, selects the
        # higher number". Both legs are persisted so a disputed breach can be
        # audited against the figure the firm used.
        if st.get('day') != day:
            if balance is None:
                balance = _fetch_balance()
            st['day'] = day
            st['day_anchor_nav']    = daily_base(balance, nav)
            st['day_base_balance']  = balance
            st['day_base_equity']   = nav
            st['day_low_nav'] = nav

        st['peak_nav']    = max(st.get('peak_nav', nav), nav)
        st['day_low_nav'] = min(st.get('day_low_nav', nav), nav)
        st['last_nav']    = nav
        st['last_updated'] = now.isoformat()

        peak   = st['peak_nav'] if PEAK_ANCHOR == 'peak' else st['start_nav']
        anchor = st['day_anchor_nav']

        total_dd_now   = (nav - peak) / peak if peak else 0.0
        daily_dd_now   = (nav - anchor) / anchor if anchor else 0.0
        daily_dd_worst = (st['day_low_nav'] - anchor) / anchor if anchor else 0.0

        st['max_total_dd']   = min(st.get('max_total_dd', 0.0), total_dd_now)
        st['worst_daily_dd'] = min(st.get('worst_daily_dd', 0.0), daily_dd_worst)

        _save_state(st)

        start = st['start_nav']
        gain = (nav - start) / start if start else 0.0
        return {
            'nav': nav, 'anchor_nav': peak, 'start_nav': start, 'day_anchor': anchor,
            # The intraday low, and the two legs the base was chosen from. The
            # limit is breached "at any point during the day", so the low — not
            # the sampled tick — is what a halt must be judged on.
            'day_low': st['day_low_nav'],
            'day_base_balance': st.get('day_base_balance'),
            'day_base_equity': st.get('day_base_equity'),
            'total_dd_now': total_dd_now,
            'daily_dd_now': daily_dd_now,
            'daily_dd_worst': daily_dd_worst,
            'max_total_dd': st['max_total_dd'],
            'worst_daily_dd_all': st['worst_daily_dd'],
            'gain': gain,
        }
    except Exception as e:
        print(f'[prop_guard] update failed: {e}', file=sys.stderr)
        return {}


def _status_icon(dd: float, limit: float) -> str:
    used = abs(dd) / limit if limit else 0
    if used >= 1.0:
        return '🚨'
    if used >= WARN_FRACTION:
        return '⚠️'
    return '✅'


def _base_legs(m: dict) -> str:
    """How today's base was chosen — or that it predates the snapshot.

    A state file written before the max(balance, equity) rule landed carries an
    anchor but neither leg. Rendering those as $0 would read as a real reading of
    zero balance; say so instead. Self-heals at the next roll.
    """
    bal, eq = m.get('day_base_balance'), m.get('day_base_equity')
    if bal is None and eq is None:
        return '(equity-only anchor — re-snapshots at the next roll)'
    return (f"= max(bal ${bal:,.0f}, eq ${eq:,.0f})" if bal is not None and eq is not None
            else f"= {'bal' if bal is not None else 'eq'} ${bal if bal is not None else eq:,.0f}")


def report_section(m: dict = None) -> str:
    """Formatted Telegram section. Computes fresh metrics if none supplied."""
    if m is None:
        m = update()
    if not m:
        return '🛡 <b>Prop Limits</b>\n  (account NAV unavailable)'
    di = _status_icon(m['daily_dd_worst'], DAILY_DD_LIMIT)
    ti = _status_icon(m['total_dd_now'], TOTAL_DD_LIMIT)
    anchor_label = 'start' if PEAK_ANCHOR == 'start' else 'peak'
    prof_used = m['gain'] / PROFIT_TARGET if PROFIT_TARGET else 0
    pi = '🎯' if m['gain'] >= PROFIT_TARGET else '📈'
    return (
        '🛡 <b>Prop Limits</b> (FTMO 2-step / The5ers — open equity)\n'
        f"  NAV: ${m['nav']:,.0f}  (start ${m['start_nav']:,.0f})\n"
        f"  Daily: {m['daily_dd_now']*100:+.2f}% (worst today {m['daily_dd_worst']*100:+.2f}%) "
        f"/ -{DAILY_DD_LIMIT*100:.0f}% {di}\n"
        f"  Base:  ${m['day_anchor']:,.0f} {_base_legs(m)} — "
        f"floor ${m['day_anchor']*(1-DAILY_DD_LIMIT):,.0f}\n"
        f"  Total: {m['total_dd_now']*100:+.2f}% from {anchor_label} / -{TOTAL_DD_LIMIT*100:.0f}% {ti}\n"
        f"  Profit: {m['gain']*100:+.2f}% / +{PROFIT_TARGET*100:.0f}% target ({prof_used*100:.0f}%) {pi}\n"
        f"  Worst-ever: daily {m['worst_daily_dd_all']*100:+.2f}%, total {m['max_total_dd']*100:+.2f}%"
    )


def _maybe_alert(m: dict) -> None:
    """Send a Telegram alert when a drawdown crosses the warn/breach threshold,
    de-duplicated per day so it doesn't spam every run."""
    if not m:
        return
    daily_used = abs(m['daily_dd_worst']) / DAILY_DD_LIMIT
    total_used = abs(m['total_dd_now']) / TOTAL_DD_LIMIT
    level = None
    if daily_used >= 1.0 or total_used >= 1.0:
        level = 'breach'
    elif daily_used >= WARN_FRACTION or total_used >= WARN_FRACTION:
        level = 'warn'
    if level is None:
        return
    st = _load_state()
    key = f"{st.get('day')}:{level}"
    if st.get('last_alert_key') == key:
        return  # already alerted at this level today
    try:
        from telegram_bot import notify_html
        emoji = '🚨' if level == 'breach' else '⚠️'
        notify_html(
            f"{emoji} <b>PROP DD {level.upper()}</b>\n"
            f"Daily: {m['daily_dd_worst']*100:+.2f}% / -{DAILY_DD_LIMIT*100:.0f}% "
            f"({daily_used*100:.0f}% used)\n"
            f"Total: {m['total_dd_now']*100:+.2f}% / -{TOTAL_DD_LIMIT*100:.0f}% "
            f"({total_used*100:.0f}% used)\n"
            f"NAV: ${m['nav']:,.0f}"
        )
        st['last_alert_key'] = key
        _save_state(st)
    except Exception as e:
        print(f'[prop_guard] alert failed: {e}', file=sys.stderr)


def _update_halt_flag(m: dict) -> None:
    """Write trading_halt.flag when a DD reaches HALT_FRACTION of its limit;
    remove it once BOTH daily and total are back under. No-op unless
    PROP_GUARD_HALT=1. live_test reads the flag before every order."""
    if not HALT_ENABLED or not m:
        return
    daily_used = abs(m['daily_dd_worst']) / DAILY_DD_LIMIT
    total_used = abs(m['total_dd_now']) / TOTAL_DD_LIMIT
    breached = daily_used >= HALT_FRACTION or total_used >= HALT_FRACTION
    try:
        if breached:
            which = []
            if daily_used >= HALT_FRACTION: which.append(f'daily {m["daily_dd_worst"]*100:+.2f}%')
            if total_used >= HALT_FRACTION: which.append(f'total {m["total_dd_now"]*100:+.2f}%')
            payload = {
                'flatten': HALT_FLATTEN,
                'reason': 'DD ' + ' & '.join(which) + f' >= {HALT_FRACTION*100:.0f}% of limit',
                'daily_dd_worst': m['daily_dd_worst'], 'total_dd_now': m['total_dd_now'],
                'nav': m['nav'], 'ts': datetime.now(timezone.utc).isoformat(),
            }
            with open(HALT_FLAG_FILE, 'w') as fh:
                json.dump(payload, fh, indent=1)
            print(f'[prop_guard] ⛔ HALT flag WRITTEN ({payload["reason"]}, '
                  f'flatten={HALT_FLATTEN})', file=sys.stderr)
        elif os.path.exists(HALT_FLAG_FILE):
            os.remove(HALT_FLAG_FILE)
            print('[prop_guard] ✅ HALT flag cleared (DD recovered under threshold)', file=sys.stderr)
    except Exception as e:
        print(f'[prop_guard] halt-flag update failed: {e}', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description='Prop-firm account drawdown monitor')
    ap.add_argument('--quiet', action='store_true', help='update state silently (for schedulers)')
    args = ap.parse_args()
    m = update()
    _maybe_alert(m)
    _update_halt_flag(m)
    if not args.quiet:
        print(report_section(m))


if __name__ == '__main__':
    main()
