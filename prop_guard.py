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
from datetime import datetime, timezone

import requests

OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN  = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL   = 'https://api-fxpractice.oanda.com'

STATE_FILE = os.path.join(os.path.dirname(__file__), 'prop_guard_state.json')

# --- Prop-firm limits (fractions of the relevant anchor) ---
# Configured for FTMO 2-Step and The5ers High Stakes, which share the same
# risk structure: 5% daily loss + 10% STATIC max loss from the initial balance
# (neither trails up from peak), with a 10% Phase-1 / 5% Phase-2 profit target.
# Both measure intraday on equity (open positions count toward the limits).
DAILY_DD_LIMIT = 0.05    # 5% per-day loss limit (FTMO 2-step & The5ers High Stakes)
TOTAL_DD_LIMIT = 0.10    # 10% max loss — STATIC, from starting balance
PROFIT_TARGET  = 0.10    # Phase-1 profit target (Phase 2 is 5%); informational
WARN_FRACTION  = 0.70    # alert once a drawdown reaches this fraction of its limit
PEAK_ANCHOR    = 'start'  # 'start' = static-from-initial (FTMO 2-step / The5ers);
                          # 'peak'  = trailing-from-peak (FTMO 1-step, futures-style)

# Daily reset boundary (UTC hour). FTMO resets at 00:00 CE(S)T (= 22:00 UTC in
# summer / 23:00 UTC in winter); The5ers resets at 00:00 broker-server time.
# 22 ≈ 00:00 CEST (current season). Adjust for winter / your broker's server TZ.
DAY_RESET_UTC_HOUR = 22


def _fetch_nav():
    """Return current account NAV (balance + unrealized P&L), or None on failure."""
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


def _trading_day(now: datetime) -> str:
    """Return the YYYY-MM-DD label for the current trading day given the reset hour."""
    # Shift the clock back by the reset hour so the 'day' rolls over at that UTC hour.
    shifted = now.timestamp() - DAY_RESET_UTC_HOUR * 3600
    return datetime.fromtimestamp(shifted, tz=timezone.utc).strftime('%Y-%m-%d')


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


def update(nav: float = None) -> dict:
    """Fetch NAV (if not supplied), update the persisted DD state, and return a
    metrics dict. Safe to call frequently — it's the high-frequency sampler that
    keeps peak / daily-low accurate. Never raises into a caller."""
    try:
        if nav is None:
            nav = _fetch_nav()
        if nav is None:
            return {}

        st = _load_state()
        now = datetime.now(timezone.utc)
        day = _trading_day(now)

        # First run — seed everything from current NAV.
        if not st:
            st = {'peak_nav': nav, 'start_nav': nav, 'day': day,
                  'day_anchor_nav': nav, 'day_low_nav': nav,
                  'max_total_dd': 0.0, 'worst_daily_dd': 0.0}

        # New trading day — reset the daily anchor and intraday low.
        if st.get('day') != day:
            st['day'] = day
            st['day_anchor_nav'] = nav
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


def main():
    ap = argparse.ArgumentParser(description='Prop-firm account drawdown monitor')
    ap.add_argument('--quiet', action='store_true', help='update state silently (for schedulers)')
    args = ap.parse_args()
    m = update()
    _maybe_alert(m)
    if not args.quiet:
        print(report_section(m))


if __name__ == '__main__':
    main()
