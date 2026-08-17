#!/usr/bin/env python3
"""Single-process, non-netting, HEDGING FIX runner for The5ers (cTrader).

One process, one FIX session, all cTrader-tradeable paper_trading sleeves. Each
sleeve owns its own position by PosID (hedging): open -> store PosID; flip/exit ->
close_position(that PosID) then open new. Reuses the existing strategy signal +
sizing; only execute/close is FIX. OANDA netting paper book is untouched.

    ./venv/bin/python fix_runner.py --once            # DRY-RUN one pass (no orders)
    ./venv/bin/python fix_runner.py                   # DRY-RUN loop
    ./venv/bin/python fix_runner.py --live            # place real orders

ponytail ceilings (fill before heavy live use):
  * cTrader per-symbol min/step VOLUME isn't wired — units are the risk-model value
    clipped to a coarse floor. Pull real min/step from the cTrader symbol specs
    (SecurityList 1008/volume fields) before trusting live sizing precision.
  * daily equity reconcile to the broker's real balance is a TODO hook (reconcile()).
"""
import os, sys, json, time, argparse
from datetime import datetime, timedelta, timezone
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validator import create_strategy_function
from pipeline_utils import init_db
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data
import portfolio as P
import kelly_policy
from fix_adapter import FixAdapter, _FIX_SYMBOL_ID, FIX_START_EQUITY, _MIN_VOL
from ctrader_adapter import OandaAdapter          # OANDA is the DATA source (price/candles)

# Base risk per trade — the ONLY term that scales the whole book uniformly. Everything
# else in size_units() is per-sleeve (ws, kelly, corr, decay) or a clamp (MAXRISK), so
# this is the knob you move to hold book magnitude constant when the sleeve count
# changes (_apply_cluster_caps drops the risk a binding cap frees instead of
# redistributing it, so N is a magnitude lever too).
#
# BASE_RISK is the ONLY name. FIX_RISK used to be honoured as a fallback because the
# Zeabur dashboard still said FIX_RISK while .env had moved on, and the pod env is a
# hand-maintained list that drifts from .env silently — dropping the old name then would
# have sized the live book off the code default. That migration is COMPLETE: the pod env
# carries BASE_RISK and no FIX_RISK (verified 2026-08-08), so the alias is retired per the
# plan recorded here.
#
# The alias was not free. .env ended up holding BOTH names DISAGREEING (BASE_RISK=0.002
# against FIX_RISK=0.005), so a local runner silently sized at 0.2% while the pod ran
# 0.5%, and deleting one line would have moved the book with nothing in the logs. One
# name cannot drift from itself.
#
# Separate from OANDA's RISK_PER_TRADE, which sizes the paper book.
RISK = float(os.getenv('BASE_RISK') or '0.005')
MAXRISK = float(os.getenv('FIX_MAXRISK', '0.02'))   # per-trade hard cap; skip open if min-lot exceeds it

# Book-magnitude scale, applied on top of BASE_RISK. Separate knob, ONE job:
# BASE_RISK is the per-trade risk budget ("0.5% per position") and BOOK_SCALE is
# how hot the whole book runs. Keeping them apart means the sizing decision and
# the book decision can be reasoned about — and changed — independently.
#
# BE CLEAR ABOUT WHAT THIS IS: it MULTIPLIES BASE_RISK. `BOOK_SCALE=1.10` at
# BASE_RISK 0.005 is byte-identical to BASE_RISK 0.0055 at BOOK_SCALE 1.0 —
# verified over 674 bars, max equity difference 0.0. It is NOT an extra
# dimension of risk, it is the same lever with a clearer name, so setting BOTH
# compounds. The effective figure is printed at startup for exactly that reason.
#
# It lives HERE and not in portfolio.py's weight vector on purpose: live_test
# treats "weights don't sum to 1.0" as evidence of an upstream bug and clamps on
# it (live_test.py:265), so scaling the written weights would trip a real safety
# net and blind it to the bug it exists to catch.
BOOK_SCALE = float(os.getenv('BOOK_SCALE', '1.0'))
EFF_RISK = RISK * BOOK_SCALE                        # what actually sizes a trade
# Execution venue. 'fix' (default) keeps the debugged FIX path; 'ctrader' routes orders
# over the Open API, where the stop is ATTACHED to the position rather than being a
# separate order that can outlive it. Flip with VENUE=ctrader in .env; rollback is
# unsetting it and restarting.
VENUE = os.getenv('VENUE', 'fix').strip().lower()
DEFAULT_STOP_MULT = 2.0
# Kelly constants live in kelly_policy — this book and live_test held separate
# copies that nothing kept in sync. Re-exported so existing readers still work.
KELLY_WIN, KELLY_MIN, KELLY_UP = (kelly_policy.ACTIVE_WINDOW,
                                  kelly_policy.MIN_TRADES,
                                  kelly_policy.UP)
STATE_FILE = os.path.join(os.path.dirname(__file__), 'fix_runner_state.json')

# Trigger-driven scheduling. RUNNER_MODE=cron makes the runner NEVER start a pass on its
# own — no pass on boot, no internal --at schedule — so a redeploy stops being a trading
# action. Something outside (host cron) creates TRIGGER_FILE and the runner picks it up.
#
# Why a file and not `kubectl exec fix_runner --once`: state is loaded ONCE at startup and
# written back, so a second process would clobber the resident's view and silently lose a
# pos_id. One process stays the only writer. The file also waits on disk, so a pod that is
# mid-restart at the trigger time runs the pass late instead of skipping the day.
#
# Both paths resolve through realpath because /app/fix_runner_state.json is a symlink to
# the mounted volume — the trigger and receipt must land on /data, not in the image.
RUNNER_MODE   = os.getenv('RUNNER_MODE', '').strip().lower()
_STATE_DIR    = os.path.dirname(os.path.realpath(STATE_FILE))
TRIGGER_FILE  = os.path.join(_STATE_DIR, 'trade_now')
RECEIPT_FILE  = os.path.join(_STATE_DIR, 'last_pass.json')
TRIGGER_POLL  = int(os.getenv('TRIGGER_POLL', '60'))

# ---------------------------------------------------------------------------
# DRAWDOWN CIRCUIT BREAKER (N3/N4)
#
# WHY IN THIS PROCESS. State is loaded once at startup (:656) and written back
# after every pass (:579), so a second process checking equity and closing
# positions would clobber the resident's in-memory view and lose a pos_id — and
# a close aimed at a dead id opens the OPPOSITE position rather than erroring.
# The cron wait loop is already awake every TRIGGER_POLL seconds, so the guard
# rides in it: no new process, no cross-filesystem flag, one state writer.
#
# WHY IT EXISTS. CLUSTER_CAP bounds each cluster (RISK x CLUSTER_CAP = 1.00%)
# and MAXRISK bounds each trade, but NOTHING bounds their sum. Measured
# 2026-08-05 on the 23-sleeve book: the arithmetic max if every sleeve stops on
# one day is 3.985% against a 3% wall, and sized-for exposure has historically
# peaked at 2.998%. The block bootstrap reports 0.00% daily breach but CANNOT
# price that day — it can only resample days that occurred. On this plan a 3%
# daily loss is PERMANENT TERMINATION, not a pause.
GUARD_ENABLED    = os.getenv('PROP_GUARD_HALT', '0') == '1'
GUARD_DAILY_LIM  = float(os.getenv('PROP_DAILY_DD_LIMIT', '0.03'))
GUARD_TOTAL_LIM  = float(os.getenv('PROP_TOTAL_DD_LIMIT', '0.10'))
GUARD_FRACTION   = float(os.getenv('PROP_HALT_FRACTION', '0.80'))   # halt at 80% of a limit
GUARD_EVERY      = int(os.getenv('PROP_GUARD_EVERY', '5'))          # sample every Nth poll
HALT_FILE        = os.path.join(_STATE_DIR, 'trading_halt.json')

# ---- ROLL-FLAT: stop paying carry on the instruments that pay the most of it ----
#
# Swap is charged only on a position held THROUGH the broker's midnight roll, so
# closing just before it and reopening after replaces a day of carry with one
# round trip. On this book that is worth +19.33% vs +5.03% over 2024-01-01..
# 2026-08-08 (risk 0.005, swap and spread charged) — the carry is 47% of gross.
#
# SCOPE IS A PER-INSTRUMENT RATIO, not a policy. Roll-flat pays a round trip in
# place of one day's carry and both legs are linear in units, so the test is
# carry/day / round-trip. RE-MEASURED 2026-08-11 on the BROKER'S OWN commission
# card (the earlier figures used pipeline_utils' OANDA card, which overcharged
# gold 4.5x and so understated its headroom):
#     NAS100 17.94x   DE30 2.78x   XAU 2.43x   SPX500 1.44x   XAG 1.39x
#     XCU 1.28x   ETH 0.85x   BTC 0.65x   every FX pair 0.38-0.79x
# Below 1.0 the round trip costs MORE than the swap it avoids, so BTC and ETH
# must never be in this set — measured, a BTC-only arm LOSES 0.17pp.
#
# XAU now clears the same bar DE30 does and is worth +1.08pp, but the DEFAULT
# BELOW IS DELIBERATELY UNCHANGED: the pod does not set ROLL_FLAT_INSTRUMENTS, so
# editing this default would change live behaviour on the next restart without an
# interlock. Add gold by SETTING THE ENV VAR, not by editing this line.
#
# DEFAULT OFF. Like VENUE, this is inert until deliberately set, so rollback is
# unsetting the env var and restarting rather than a code revert.
ROLL_FLAT        = os.getenv('ROLL_FLAT', '0') == '1'
ROLL_FLAT_INSTS  = {i.strip() for i in os.getenv(
    'ROLL_FLAT_INSTRUMENTS', 'NAS100_USD,DE30_EUR,SPX500_USD').split(',') if i.strip()}
# Minutes before the broker's midnight to close in. The index session shuts 10
# minutes BEFORE the roll in both DST regimes (summer 20:50 UTC, winter 21:50),
# so the window is the last sliver of the session, not a UTC constant — a fixed
# UTC time pays the whole carry for the ~4.5 months the offset is +2 instead of
# +3, and nothing would report it.
ROLL_FLAT_LEAD   = int(os.getenv('ROLL_FLAT_LEAD_MIN', '20'))
# Stop trying this many minutes BEFORE the deadline. A close that fills at
# 23:49:59 is a fill; one that arrives at 23:50:01 is a reject, and a rejected
# close after the stop has been cancelled is how the position ended up bare on
# 2026-08-10.
ROLL_FLAT_GRACE  = int(os.getenv('ROLL_FLAT_GRACE_MIN', '3'))
ROLL_FLAT_FILE   = os.path.join(_STATE_DIR, 'roll_flat_state.json')

# ---- WEEKEND-FLAT: surrender the position over the weekend ----
#
# A DIFFERENT TRADE FROM ROLL-FLAT, not a longer version of it. Roll-flat swaps one
# day's carry for one round trip and keeps the exposure; weekend-flat SURRENDERS the
# exposure until the strategy says something new. Its cost is therefore FOREGONE
# EDGE, not spread, which is why it cannot be screened with a carry/round-trip ratio
# and had to be simulated.
#
# Measured 2026-08-11 (risk_model_sim, 22 sleeves, ctrader, commission+swap charged,
# 2024-01-01..2026-08-10): weekend-flat on SPX500/XAG/XCU combined with roll-flat on
# NAS100/DE30/XAU returns +18.02% with maxDD -2.65% and worst intraday day -1.44%,
# against the roll-only book's +19.98% / -5.98% / -2.19%. It BUYS TAIL, it does not
# buy return — and the tail is what the daily wall judges. Weekend-flat alone LOSES
# 1.48pp, because sitting out weekends forgoes more edge than the carry it saves.
#
# THE CLOSE PRESERVES THE SIGNAL, AND THAT IS WHAT MAKES THE ARM SAFE. flatten_all
# is called with preserve_signal=True, so each sleeve becomes FLAT(its own signal)
# rather than FLAT(0) — acts_on_signal then returns False on every pass between the
# Friday close and the reopen. A FLAT(0) close on a Friday would instead
# re-establish on the next pass, 21:15 UTC, into a market shut until Sunday.
#
# THE REOPEN IS A SEPARATE STEP, not a flag on the close. weekend_flat_reopen runs
# on the first pass of the NEXT broker week and clears the latched sleeves to
# FLAT(0), so they act on whatever the strategy says that morning. See
# WEEKEND_FLAT_REENTRY below for why it is default on and what it costs.
#
# THE WINDOW IS SHARED WITH ROLL-FLAT ON PURPOSE. The Friday roll instant IS the
# weekly close for these instruments, so roll_flat_due already computes the right
# moment (earlier of the session close and the roll, on both clocks). Weekend-flat
# is that same window, fired only on a broker-clock Friday, latched per week.
#
# DEFAULT OFF, like ROLL_FLAT and VENUE — inert until deliberately set, so rollback
# is unsetting the env var and rolling the pod, never a code revert.
WEEKEND_FLAT       = os.getenv('WEEKEND_FLAT', '0') == '1'
# WHERE THE SCOPE CAME FROM, for the banner. An unset var and a var set to exactly
# the default are indistinguishable in the log otherwise, and the difference
# matters: the default is a CODE constant that a future edit would change silently
# on the next restart, while an explicitly-set var is under the interlock. This is
# the ambiguity that forced the "do NOT edit this default" warning above.
_RF_FROM_ENV       = os.getenv('ROLL_FLAT_INSTRUMENTS') is not None
_WF_FROM_ENV       = os.getenv('WEEKEND_FLAT_INSTRUMENTS') is not None
# Instruments whose WEEKEND carry justifies giving up the exposure. Not the same
# question as roll-flat's: this set is the one the simulation picked, not a ratio.
WEEKEND_FLAT_INSTS = {i.strip() for i in os.getenv(
    'WEEKEND_FLAT_INSTRUMENTS', 'SPX500_USD,XAG_USD,XCU_USD').split(',') if i.strip()}
WEEKEND_FLAT_FILE  = os.path.join(_STATE_DIR, 'weekend_flat_state.json')
# MONDAY RE-ENTRY, and it is DEFAULT ON — the operator's setting, chosen 2026-08-17.
#
# The leg surrenders the exposure over the weekend and takes it back at the first
# pass of the new broker week. That is a DIFFERENT POLICY from the one the
# 2026-08-11 figures were measured on, which sat out until the signal flipped, and
# the difference is not small: measured 2026-08-17 (risk_model_sim, same 22 sleeves,
# ctrader, swap+spread charged, 2024-01-01..2026-08-10) re-entry saves $4,081 of XAG
# carry for $617 of extra entry fee and beats HOLDING on every axis, but at matched
# worst-day-intraday it runs 71% more account drawdown than sitting out (-6.92% vs
# -4.05%). It is armed because it is what the operator wants the book to do, not
# because it dominates. WEEKEND_FLAT_REENTRY=0 restores the sit-out without a code
# revert — the same rollback shape as WEEKEND_FLAT and ROLL_FLAT themselves.
#
# WHY THIS IS NOT `preserve_signal=False`. Clearing the signal at the Friday close
# would re-establish on the NEXT pass, 21:15 UTC Friday, into a market shut until
# Sunday — the hazard the close's docstring calls out. The reopen is therefore a
# separate, explicit step that runs on a LATER broker day, off the latch the close
# already writes. No new state, and the shut-market window is skipped by
# construction rather than by scheduling.
WEEKEND_FLAT_REENTRY = os.getenv('WEEKEND_FLAT_REENTRY', '1') == '1'


def halt_decision(equity, day_anchor, start_equity,
                  daily_limit=None, total_limit=None, fraction=None,
                  day_low=None):
    """-> None | 'daily' | 'total'. Pure: no clock, no I/O, no broker.

    Both drawdowns are measured on EQUITY INCLUDING OPEN POSITIONS, because that
    is what the firm measures — a floating loss counts against the limit exactly
    like a realised one.

    `day_anchor` is the daily BASE: max(balance, equity) snapshotted at midnight
    server time (prop_guard.daily_base), not the equity at whatever moment we
    happened to sample.

    `day_low` is the lowest equity seen today, and it — not `equity` — is what the
    daily test uses. The rule breaches "if your equity drops below your calculated
    daily threshold AT ANY POINT during the day", so comparing only the currently
    sampled tick misses a dip that recovered between samples: the account is gone
    and the guard never saw it. Defaults to `equity` so a caller without a
    persisted low degrades to the old behaviour rather than to no check.

    Total is checked FIRST and is the more serious verdict: the daily anchor
    resets every session, the static total never does, so a total breach must not
    be reported as the recoverable one.
    """
    daily_limit = GUARD_DAILY_LIM if daily_limit is None else daily_limit
    total_limit = GUARD_TOTAL_LIM if total_limit is None else total_limit
    fraction    = GUARD_FRACTION if fraction is None else fraction
    if not equity or not day_anchor or not start_equity:
        return None                                  # unknown != safe, but unknown != breach
    low = equity if not day_low else min(float(day_low), float(equity))
    # EPS because the thresholds are products of decimals that have no exact
    # binary form: 0.03 * 0.80 is -0.024000000000000004, so an equity exactly
    # 2.40% down compared as strictly-greater and did NOT halt. On a threshold
    # whose whole job is firing before a permanent termination, round toward
    # halting.
    eps = 1e-9
    if (low - start_equity) / start_equity <= -abs(total_limit) * fraction + eps:
        return 'total'
    if (low - day_anchor) / day_anchor <= -abs(daily_limit) * fraction + eps:
        return 'daily'
    return None


def halt_is_active(halt, today):
    """Is a recorded halt still binding on `today`?

    A DAILY halt is a PAUSE: it latches for the rest of the trading day and
    lifts when the broker's day rolls, so the book re-establishes on the next
    pass. A TOTAL halt never lifts on its own — the static limit does not reset,
    so re-entering after one walks straight back at the account limit. Clearing
    it is a human decision: delete trading_halt.json.
    """
    if not halt:
        return False
    if halt.get('kind') == 'total':
        return True
    return halt.get('day') == today

# cTrader (min_volume, step) in base-ccy/contract units. FIX SecurityList does NOT
# carry volume specs (only id/name/digits) — these are the Open API's minVolume/
# stepVolume. <<VERIFY EACH against The5ers' cTrader symbol specifications before
# heavy live use; wrong step -> rejected order or mis-size. Applied values are logged.>>
VOL_SPEC = {
    'EUR_USD': (1000, 1000), 'GBP_USD': (1000, 1000), 'USD_CHF': (1000, 1000),
    'GBP_JPY': (1000, 1000), 'EUR_JPY': (1000, 1000), 'EUR_GBP': (1000, 1000),   # FX: 1000 = 0.01 lot
    'XAU_USD': (1, 1), 'XAG_USD': (50, 50), 'XPT_USD': (1, 1), 'XPD_USD': (1, 1),
    'XCU_USD': (2000, 2000),   # copper min is 0.02 lot (not 0.01) per The5ers; locked <\$20k anyway
    # indices: min 0.01 lot confirmed, but FIX units-per-lot NOT derivable here (1 contract? 100?).
    # <<CONFIRM on the first live index fill — the ExecReport shows the accepted 38(qty).>>
    'NAS100_USD': (0.01, 0.01), 'SPX500_USD': (0.01, 0.01), 'DE30_EUR': (0.01, 0.01),
    'AU200_AUD': (0.01, 0.01), 'HK33_HKD': (0.01, 0.01),
    'WTICO_USD': (10, 10), 'NATGAS_USD': (100, 100), 'BTC_USD': (0.01, 0.01), 'ETH_USD': (0.01, 0.01),
}
# Under VENUE=ctrader the venue itself reports authoritative minVolume/stepVolume, so
# VOL_SPEC above (hand-maintained, and wrong on WTICO_USD 10-vs-1 and XCU_USD 2000-vs-250)
# must NOT be used for sizing. Loading it here keeps round_vol and min_lot_implied_risk on
# the SAME number the order is actually placed at — otherwise the MAXRISK gate reasons
# from a guess while ctrader_exec places at the real minimum.
_CT_VOL_SPEC = {}
if VENUE == 'ctrader':
    _ctpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ctrader_symbols.json')
    _ctd = json.load(open(_ctpath))
    for _sec in ('instruments', 'unmapped_but_available'):
        for _i, _spec in _ctd.get(_sec, {}).items():
            # wire volume is centi-units; sizing works in units
            _CT_VOL_SPEC[_i] = (_spec['min_volume'] / 100.0, _spec['step_volume'] / 100.0)
    print(f"VENUE=ctrader — volume specs from the venue for {len(_CT_VOL_SPEC)} instruments")


def round_vol(units, inst):
    """VOL_SPEC is the hand-maintained fallback; a min volume LEARNED from a broker reject
    (fix_adapter._MIN_VOL) always wins, so sizing self-corrects per instrument. NOTE this fixes
    the min/step only — it cannot detect a wrong units BASIS (if 1 FIX unit isn't 1 price-unit,
    risk would be mis-scaled); confirm that from the first fill's accepted qty vs the lot size."""
    if VENUE == 'ctrader':
        if inst not in _CT_VOL_SPEC:
            # Loud on purpose: silently sizing off the guess table is the bug this removes.
            raise KeyError('no cTrader volume spec for %s — rebuild ctrader_symbols.json' % inst)
        mn, st = _CT_VOL_SPEC[inst]
    else:
        mn, st = VOL_SPEC.get(inst, (1, 1))
        learned = _MIN_VOL.get(_FIX_SYMBOL_ID.get(inst))   # learned from a FIX reject
        if learned:
            mn = st = learned
    return max(round(units / st) * st, mn), (mn, st)

_q2u_cache = {}
_CONV = {'JPY': ('USD_JPY', True), 'CHF': ('USD_CHF', True), 'CAD': ('USD_CAD', True),
         'HKD': ('USD_HKD', True), 'EUR': ('EUR_USD', False), 'GBP': ('GBP_USD', False),
         'AUD': ('AUD_USD', False)}
_FALLBACK = {'JPY': 1/150., 'CHF': 1/0.91, 'CAD': 1/1.37, 'HKD': 1/7.80,
             'EUR': 1.08, 'GBP': 1.25, 'AUD': 0.66}
def q2usd(inst):
    """Value of 1 quote-ccy unit in USD, from OANDA DATA (FIX has no quote session).
    USD-quoted -> 1.0 exact; else the conversion pair's latest OANDA close (fallback const)."""
    q = inst.split('_')[1]
    if q == 'USD': return 1.0
    if q in _q2u_cache: return _q2u_cache[q]
    pair, invert = _CONV.get(q, (None, False))
    r = _FALLBACK.get(q, 1.0)
    if pair:
        try:
            end = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
            start = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
            px = float(get_candles_date_range(pair, start, end, granularity='D')['close'].iloc[-1])
            r = (1.0 / px) if invert else px
        except Exception:
            pass
    _q2u_cache[q] = r
    return r

def _atr(df, n=14):
    tr = pd.concat([(df['high']-df['low']),(df['high']-df['close'].shift(1)).abs(),
                    (df['low']-df['close'].shift(1)).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean().iloc[-1]

def _rolling_kelly(raw):
    """Delegates to kelly_policy so this book and live_test cannot drift apart.
    Behaviour is unchanged — verified identical on all 24 tradeable sleeves."""
    return kelly_policy.kelly_multiplier(raw)

def load_sleeves():
    """paper_trading sleeves whose instrument cTrader offers, with portfolio scaling.

    Called ONCE, outside the run loop, and that is deliberate — do NOT "fix" it to
    re-read per pass the way live_test now does. This reads
    /app/portfolio_state.json, which is baked into the IMAGE: the mounted volume is
    /data (pipeline.db, fix_runner_state.json, trade_now) and the pod never runs
    portfolio.py. The file therefore cannot change for the lifetime of the pod, so
    re-reading it would return an identical answer every pass while adding the
    silent-resize hazards live_test had to guard against.

    Getting a fresh decay/weight verdict onto the prop book is a DELIVERY problem
    (a push, or shipping state to /data), not a caching one — and auto-refreshing
    live weights from off-pod would remove the human checkpoint that every other
    guardrail here depends on.
    """
    wdict = json.load(open(os.path.join(os.path.dirname(__file__),'portfolio_state.json')))
    N, W = wdict['n_strategies'], wdict['weights']
    decay = wdict.get('decay_kelly_scale', {})
    corr_peers = {}
    for pair in wdict.get('correlated_pairs', []):
        a, b = pair.get('a'), pair.get('b')
        if a and b:
            corr_peers.setdefault(a, set()).add(b)
            corr_peers.setdefault(b, set()).add(a)
    out, skipped = [], []
    for row in P.load_strategies():
        sid = row['id']
        if row['status'] != 'paper_trading' or (row['timeframe'] or 'D') != 'D':
            continue
        inst = row.get('instrument') or P._infer_instrument(sid)  # DB column is authoritative;
        if inst not in _FIX_SYMBOL_ID:                            # infer only as fallback

            skipped.append((sid, f'{inst} not on cTrader')); continue
        if sid not in W:
            skipped.append((sid, 'no weight')); continue
        out.append(dict(sid=sid, inst=inst, code=row['code'],
                        params=json.loads(row['best_params'] or '{}'),
                        arch=P._infer_archetype(row['code'], row.get('archetype') or 'standard'),
                        instrument2=row.get('instrument2'),
                        ws=min(W[sid]*N, 3.0),
                        decay_kelly_scale=float(decay.get(sid, 1.0)),
                        corr_peers=sorted(corr_peers.get(sid, set())),
                        fn=create_strategy_function(row['code'])))
    return out, skipped

def _corr_scale(sleeve, state):
    sig = state.get(sleeve['sid'], {}).get('signal', 0)
    if sig == 0:
        return 1.0
    for peer in sleeve.get('corr_peers', []):
        if state.get(peer, {}).get('signal', 0) == sig:
            return 0.5
    return 1.0

def latest(sleeve):
    """Return (signal, close, atr, kelly) from recent candles, or None on error."""
    inst = sleeve['inst']
    end = (datetime.utcnow()-timedelta(days=1)).strftime('%Y-%m-%d')
    start = (datetime.utcnow()-timedelta(days=1825)).strftime('%Y-%m-%d')
    df = get_candles_date_range(inst, start, end, granularity='D').reset_index(drop=True)
    df['date'] = pd.to_datetime(df['date'])
    if sleeve['arch'] != 'standard':
        df = inject_supplementary_data(df, sleeve['arch'], inst, sleeve['instrument2'], start, end, 'D')
    sig = np.asarray(sleeve['fn'](df, sleeve['params'])).astype(float)
    raw = (pd.Series(sig).shift(1).fillna(0).values * df['close'].pct_change().fillna(0).values)
    return int(np.sign(sig[-1])), float(df['close'].iloc[-1]), float(_atr(df)), _rolling_kelly(raw)

def size_units(sleeve, atr, equity, kelly, corr_scale=1.0):
    stop_mult = sleeve['params'].get('stop_mult', DEFAULT_STOP_MULT)
    eff = min(EFF_RISK * sleeve['ws'] * corr_scale * kelly * sleeve.get('decay_kelly_scale', 1.0), MAXRISK)
    raw = equity * eff / (stop_mult * atr * q2usd(sleeve['inst']))
    return round_vol(raw, sleeve['inst'])              # -> (volume, (min, step))


def min_lot_implied_risk(sleeve, atr, equity):
    stop_mult = sleeve['params'].get('stop_mult', DEFAULT_STOP_MULT)
    units, spec = round_vol(0, sleeve['inst'])
    implied = units * stop_mult * atr * q2usd(sleeve['inst']) / max(equity, 1e-9)
    return units, spec, implied

FLAT = lambda sig=0: {'signal': sig, 'pos_id': None, 'units': 0.0, 'side': 0, 'stop': None, 'stop_ref': None}


def acts_on_signal(sig, st):
    """Does this sleeve act this pass? Entries and exits fire on a signal CHANGE.

    `st['signal']` carries the entire memory of WHY a sleeve is flat, and the two
    kinds of flat are one field rather than two flags that could contradict each
    other:

      FLAT(st['signal'])  a broker stop fired, a soft stop fired, or the position
                          was closed at the broker. The signal is PRESERVED, so
                          this returns False until the strategy genuinely says
                          something new. That is what keeps the runner aligned
                          with the validated return stream, which models a fired
                          stop as flat-until-the-signal-changes.

      FLAT(0)             a DELIBERATE close — the guard's halt today, and the
                          roll-flat policy. The signal is CLEARED, so 0 -> sig
                          reads as a change and the next pass re-establishes.

    Because both live in ONE field, a sleeve can never be 'stopped out' and
    'deliberately flat' at the same time — the mutual exclusion is structural, not
    a rule someone has to remember. Consequently a policy close needs NO new
    state: close the position and write FLAT(0).

    Strict subscript, not .get(): a state entry without 'signal' is corrupt, and
    the per-sleeve try/except in run_once should catch it and skip that sleeve.
    Defaulting would silently turn a corrupt entry into an ENTRY.
    """
    return sig != st['signal']

def _refresh_marks(adapters):
    """FIX has no quote feed, so equity() would ignore unrealized PnL and book realized in
    quote currency. Feed the session live OANDA prices + quote->USD rates (same convention as
    sizing's q2usd) for each held instrument, so equity reflects USD-converted realized+unrealized.
    Only marks instruments with an open position (bounds the OANDA price calls)."""
    if not adapters: return
    if VENUE == 'ctrader':
        return          # Open API reports a real balance; nothing to mark by hand
    fixmap = adapters['fix']; sess = next(iter(fixmap.values())).fix
    for inst, ad in fixmap.items():
        sess.rate[ad.symbol] = q2usd(inst)
        if sess.positions.get(ad.symbol):
            pad = adapters['price'].get(inst)
            try:
                px = pad.get_current_price() if pad else None
                if px: sess.quotes[ad.symbol] = px
            except Exception:
                pass                               # stale mark is better than aborting the pass

def _read_halt():
    try:
        with open(HALT_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_halt(kind, day, dd, equity):
    try:
        with open(HALT_FILE, 'w') as fh:
            json.dump({'kind': kind, 'day': day, 'dd': dd, 'equity': equity,
                       'at': datetime.utcnow().isoformat()}, fh, indent=2)
    except Exception as exc:
        print(f"  [guard] could not persist halt: {exc}", file=sys.stderr)


def _guard_equity(adapters):
    """(balance, equity) — equity INCLUDING open positions — or (None, None).

    adapters['equity'] is bare BALANCE under VENUE=ctrader (:697) because the Open
    API reports no equity field — fine for sizing, useless for a drawdown limit
    that counts floating loss. prop_guard assembles the real figure from the
    broker's own netUnrealizedPnL; import it lazily so a missing tz database
    disables the guard instead of killing the runner at import.

    Balance comes back alongside equity because the daily base is
    max(balance, equity) at the roll — see prop_guard.daily_base.
    """
    try:
        import prop_guard
        if VENUE == 'ctrader':
            return prop_guard._fetch_account_ctrader()
        eq = adapters['equity']()
        return eq, eq
    except Exception as exc:
        print(f"  [guard] equity unavailable — GUARD INACTIVE this tick: {exc}", file=sys.stderr)
        return None, None


def _carry(st, day):
    """The fields a roll-flat close must hand to its own reopen.

    ROLL-FLAT IS ONE TRADE WITH A GAP IN IT, not two trades. The simulator says so
    outright — "the POSITION is deliberately left intact: signal state, stop and
    entry are unchanged" — and every roll-flat figure in the repo is measured that
    way. The runner used to disagree: FLAT(0) nulls `stop`, so the reopen computed a
    NEW stop off the new entry price and a NEW size off the current ATR.

    That is not a trailing stop, it is a RE-ANCHORED one, and it diverges from the
    validated stream in BOTH directions. Long from 100 with a stop at 95: after a
    rise to 110 the reopen stops at 105, so a fall to 104 stops the sleeve out on a
    day the backtest never exits; after a fall to 96 the reopen stops at 91, so the
    backtest exits at 95 and live keeps bleeding. Neither path exists in the numbers
    the sizing was chosen from.

    Carrying stop/entry/units makes the runner honour the model rather than making
    the model describe an accident. `day` stamps it so a stale carry — left by a
    sleeve that errored out of its pass — can never be applied to a later trade.
    """
    return {'carry_stop': st.get('stop'), 'carry_units': st.get('units'),
            'carry_side': st.get('side'), 'carry_entry': st.get('entry'),
            'carry_day': day}


def _carry_is_fresh(carry_day, now=None):
    """Is this carry from the roll-flat close that just happened?

    THE WINDOW IS FOUR DAYS, NOT ONE, and the reason is the case that is easy to
    miss: roll-flat fires every broker night INCLUDING Friday, so its reopen is the
    Monday pass and the gap is THREE days, not one. A one-day window looked right
    against a weeknight and would have silently refused the carry every Monday —
    re-anchoring NAS100/DE30/XAU stops once a week, which is the exact defect this
    carry exists to remove. Four covers a holiday Monday too.

    It is a staleness bound, not the mechanism: the carry is consumed the moment the
    sleeve re-enters, because state[sid] is replaced wholesale. This only catches a
    carry stranded by a sleeve that threw mid-pass, so that an old trade's stop can
    never be attached to a later one.
    """
    if not carry_day:
        return False
    import prop_guard
    now = now or datetime.now(timezone.utc)
    try:
        d0 = datetime.strptime(carry_day, '%Y-%m-%d').date()
    except ValueError:
        return False
    return 0 <= (prop_guard.broker_now(now).date() - d0).days <= 4


def roll_flat_resume(st, sig, entry_ref, now=None):
    """Should this entry RESUME a roll-flat trade, or start a fresh one?

    -> ('resume', stop, units) | ('stopped', stop, None) | ('fresh', None, None)

    Pure, so the three outcomes can be tested without a broker. All of them are
    reachable on an ordinary night and getting any one wrong is silent.

      resume   the sleeve is picking its own position back up. Carried stop and
               size, so the trade the backtest is holding is the trade live holds.
      stopped  price passed the carried stop DURING THE GAP. The model exited there,
               so reopening would hold a position the validated stream has already
               closed — and the broker would reject a wrong-side stop anyway. Caller
               writes FLAT(sig): signal preserved, nothing re-enters until a genuine
               flip, exactly as a fired stop behaves everywhere else in this file.
      fresh    no usable carry — a genuine flip (direction changed, so it IS a new
               trade), a stale carry, or an ordinary entry that never roll-flatted.

    The direction test is `carry_side == sig`, not merely non-zero: a sleeve that
    flipped long->short over the gap must NOT inherit the long's stop, which would
    sit on the wrong side of the market and be rejected.

    `now` is injectable only so the freshness window can be exercised against a
    fixed clock; production passes nothing and reads the real one.
    """
    cs, cu = st.get('carry_stop'), st.get('carry_units')
    if not (cs and cu and st.get('carry_side') == sig
            and _carry_is_fresh(st.get('carry_day'), now)):
        return 'fresh', None, None
    if (sig > 0 and entry_ref <= cs) or (sig < 0 and entry_ref >= cs):
        return 'stopped', cs, None
    return 'resume', cs, cu


def flatten_all(state, adapters, live, why, only=None, tag='guard',
                preserve_signal=False, carry_day=None):
    """Close every position the runner owns. Returns (closed, failed).

    `only` restricts it to a set of instruments — the roll-flat pass closes just
    the covered ones and leaves the rest of the book alone. None means every
    position, which is what the guard's halt needs and is the default so that
    path is untouched.

    Mirrors the signal-change close path exactly: cancel the broker stop FIRST and
    ABORT that sleeve if the cancel is unconfirmed, because a stop outliving its
    position fires as a naked entry in the opposite direction.

    `preserve_signal` chooses WHICH KIND OF FLAT this is, per acts_on_signal's
    contract, and the two are not interchangeable:

      False (default)  FLAT(0) — a PAUSE. The signal is cleared, so the next pass
                       reads 0 -> sig as a change and re-establishes. This is what
                       the guard's halt and the roll-flat close need, and it is the
                       default so both of those paths are untouched.
      True             FLAT(st['signal']) — a SURRENDER. The signal is preserved, so
                       nothing re-enters until the strategy genuinely says something
                       new. This is what weekend-flat needs: a cleared signal would
                       re-establish on the next pass, which over a weekend means
                       ordering into a shut market.

    Getting this backwards is silent in both directions — a pause that preserves the
    signal sits out for weeks, and a surrender that clears it re-enters immediately —
    so it is a parameter rather than a convention.
    """
    closed, failed = [], []
    for sid, st in sorted(state.items()):
        if not st.get('pos_id'):
            continue
        inst = st.get('inst') or P._infer_instrument(sid)
        if only is not None and inst not in only:
            continue
        ad = adapters['fix'].get(inst) if adapters else None
        if live and ad is None:
            failed.append((sid, 'no adapter')); continue
        if not live:
            closed.append(sid)
            nxt = FLAT(st['signal'] if preserve_signal else 0)
            if carry_day is not None:
                nxt.update(_carry(st, carry_day))
            state[sid] = nxt
            continue
        try:
            stop_ref = st.get('stop_ref')
            cancelled = False
            if stop_ref:
                if ad.cancel_stop(stop_ref, st['side']) is None:
                    failed.append((sid, 'stop cancel unconfirmed')); continue
                cancelled = True
            ack = ad.close_position(st['pos_id'], st['units'], st['side'])
            if ack is None or ack.get('ord_status') in ('8', '4', 'C'):
                # THE STOP IS ALREADY GONE. Observed live 2026-08-10: the pre-roll
                # close ran inside the index session break, every close was
                # rejected, and the position sat unstopped at the broker for
                # nearly three hours while this line reported "still stopped".
                # A cancel that is not followed by a close MUST be undone — the
                # software stop only runs during a pass, and passes are daily.
                if cancelled and st.get('stop'):
                    ref = ad.place_stop(st['pos_id'], st['units'], st['side'],
                                        st['stop'])
                    if _stop_ok(ref):
                        st['stop_ref'] = ref
                        failed.append((sid, 'close rejected — stop re-attached'))
                    else:
                        st['stop_ref'] = None
                        failed.append((sid, 'close rejected — ⚠️ STOP NOT '
                                            'RE-ATTACHED, software stop only'))
                else:
                    failed.append((sid, 'close rejected'))
                continue
            closed.append(sid)
            nxt = FLAT(st['signal'] if preserve_signal else 0)
            if carry_day is not None:
                nxt.update(_carry(st, carry_day))
            state[sid] = nxt
        except Exception as exc:
            failed.append((sid, repr(exc)))
    print(f"  [{tag}] FLATTEN ({why}): closed {len(closed)}, failed {len(failed)}")
    for sid, reason in failed:
        # The reason now carries the stop's real fate, so this no longer asserts
        # "still stopped" — which was false exactly when it mattered most.
        print(f"  [{tag}]   ⚠️ {sid} NOT closed — {reason} (still owned)")
    if live:
        json.dump(state, open(STATE_FILE, 'w'), indent=2)
    return closed, failed


def next_broker_midnight(now):
    """UTC instant of the next broker day roll — when swap is charged."""
    import prop_guard
    local = prop_guard.broker_now(now)
    return now + timedelta(minutes=(24 * 60) - (local.hour * 60 + local.minute),
                           seconds=-local.second, microseconds=-local.microsecond)


def session_end(now, intervals, tzname='Europe/Bucharest'):
    """UTC instant the instrument's CURRENT trading session shuts, or None.

    `intervals` is the broker's own schedule as (start, end) seconds from Sunday
    00:00 in `tzname` — `ProtoOASymbolByIdReq` returns exactly that. Read from
    the venue rather than hard-coded because it is the thing that invalidated the
    first version of this window.
    """
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(tzname))
    week_start = (local - timedelta(days=(local.weekday() + 1) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    here = (local - week_start).total_seconds()
    for start, end in sorted(intervals):
        if start <= here <= end:
            return (week_start + timedelta(seconds=end)).astimezone(timezone.utc)
    return None


def roll_flat_due(now, latch_day, session_end_utc=None, have_schedule=False):
    """(due, day) — is this poll inside the pre-roll close window, unlatched?

    THE DEADLINE IS WHICHEVER COMES FIRST, the swap roll or the session close.

    Version one closed in the last ten minutes before the broker's midnight and
    was rejected on every attempt of its first live night (2026-08-10): the cash
    indices trade 01:05-23:50 Europe/Bucharest, which in US summer is exactly
    20:50 UTC — the same instant that window OPENED. It sat entirely inside the
    session break, so it could never fill.

    The two clocks are genuinely different — the session is published in
    Europe/Bucharest and the swap roll follows America/New_York + 7h. They agree
    for most of the year and diverge for about four weeks (the EU and US DST
    calendars), and in that gap the session can shut AFTER the roll, so neither
    one alone is the right deadline. `min` is.

    The trading-day label comes back alongside because the latch must key on the
    same clock the deadline is derived from.
    """
    import prop_guard
    day = prop_guard.broker_now(now).strftime('%Y-%m-%d')
    if latch_day == day:
        return False, day
    # A SHUT MARKET IS NEVER DUE. This is the load-bearing clause. Anchoring the
    # window on the roll alone made it re-open after the session closed — the
    # deadline had passed, `min` fell back to the next roll 24h away, and the
    # window looked open again into a market that could only reject.
    if have_schedule and session_end_utc is None:
        return False, day
    grace, lead = ROLL_FLAT_GRACE * 60, ROLL_FLAT_LEAD * 60
    to_roll = (next_broker_midnight(now) - now).total_seconds()
    if not (grace < to_roll <= lead):
        return False, day
    # ...and stop early enough that the last attempt is still inside the session:
    # a close that fills at 23:49:59 is a fill, one arriving at 23:50:01 is a
    # reject, and a reject after the stop was cancelled is how a position ended
    # up bare on 2026-08-10.
    if session_end_utc is not None and (session_end_utc - now).total_seconds() <= grace:
        return False, day
    return True, day


_SESSION_CACHE = {}


def _session_intervals(inst, adapter):
    """The venue's own schedule for `inst`, cached for the process lifetime.

    Returns [] when it cannot be read, and the caller then falls back to the roll
    alone — which is the OLD behaviour, so a schedule fetch that fails degrades
    to a window that may be rejected, never to one that trades at the wrong time.
    """
    if inst in _SESSION_CACHE:
        return _SESSION_CACHE[inst]
    ivs = []
    try:
        if adapter is not None and hasattr(adapter, 'session_intervals'):
            ivs = adapter.session_intervals() or []
    except Exception as exc:
        print(f"  [roll-flat] schedule for {inst} unavailable: {exc}", file=sys.stderr)
    _SESSION_CACHE[inst] = ivs
    return ivs


def _read_roll_flat_latch():
    try:
        with open(ROLL_FLAT_FILE) as fh:
            return json.load(fh).get('day')
    except Exception:
        return None


def roll_flat_close(state, adapters, live, now=None):
    """Close the covered positions before the broker's midnight roll.

    Returns (closed, failed) or None when this poll is not the moment.

    ON A REJECTION the position stays open, stays in state and stays stopped —
    identical to the guard's flatten, and the sleeve simply carries one night of
    swap. The latch is only written when EVERY covered position closed, so a
    partial failure is retried on the next poll for as long as the window lasts.
    When the window passes, the miss is accepted and logged: a retry after the
    roll would pay the round trip AND the carry.

    Deliberately does not reopen. The reopen is the next ordinary trading pass,
    which reads FLAT(0) as a change and re-establishes.
    """
    if not ROLL_FLAT:
        return None
    now = now or datetime.now(timezone.utc)
    # The deadline is the EARLIEST session close among the covered instruments,
    # capped by the roll. They share a schedule today, but taking the min means a
    # future instrument with a shorter session cannot silently miss its window.
    ends, have_schedule = [], False
    for inst in sorted(ROLL_FLAT_INSTS):
        ad = adapters['fix'].get(inst) if adapters else None
        ivs = _session_intervals(inst, ad)
        if ivs:
            have_schedule = True
            e = session_end(now, ivs)
            if e:
                ends.append(e)
    due, day = roll_flat_due(now, _read_roll_flat_latch(),
                             min(ends) if ends else None, have_schedule)
    if not due:
        return None
    closed, failed = flatten_all(state, adapters, live,
                                 f'roll-flat {day}', only=ROLL_FLAT_INSTS,
                                 tag='roll-flat', carry_day=day)
    if failed:
        print(f"  [roll-flat] {len(failed)} not closed — retrying while the "
              f"window lasts; each miss carries one night of swap")
        return closed, failed
    if live:
        try:
            with open(ROLL_FLAT_FILE, 'w') as fh:
                json.dump({'day': day, 'closed': closed,
                           'at': now.isoformat()}, fh, indent=1)
        except Exception as exc:
            # A lost latch re-runs the close, which finds nothing open and is a
            # no-op — strictly safer than skipping the close entirely.
            print(f"  [roll-flat] could not persist latch: {exc}", file=sys.stderr)
    return closed, failed


def _read_weekend_flat_latch():
    try:
        with open(WEEKEND_FLAT_FILE) as fh:
            return json.load(fh).get('day')
    except Exception:
        return None


def weekend_flat_close(state, adapters, live, now=None):
    """Close the covered positions before the weekly close, and do NOT reopen.

    Returns (closed, failed) or None when this poll is not the moment.

    FIRES ONLY ON A BROKER-CLOCK FRIDAY, inside the same pre-roll window roll-flat
    uses — because for these instruments the Friday roll instant IS the weekly
    close, so the deadline arithmetic (earlier of the session close and the roll,
    across both clocks) is already correct and is reused rather than re-derived.
    The broker clock is America/New_York + 7h, so 20:50 UTC on a Friday is 23:50
    Friday there and the day label stays Friday through the whole window.

    THE POSITION IS NOT REOPENED HERE, by construction rather than by scheduling.
    preserve_signal=True writes FLAT(the sleeve's own signal), so acts_on_signal
    returns False for every pass that follows — which is why this arm does not have
    roll-flat's shut-market hazard: there is no next-pass re-entry to land on a
    closed market. Taking the exposure back is weekend_flat_reopen's job, and it
    runs on a later broker day. With WEEKEND_FLAT_REENTRY=0 nothing reopens it and
    the sleeve waits for a genuine signal flip, which may be weeks.

    ON A REJECTION nothing is left bare: flatten_all re-attaches the stop it
    cancelled and reports the real fate, and the latch is only written when every
    covered position closed, so a partial failure retries while the window lasts.
    Past the window the miss is accepted — the position carries one weekend of
    carry, which is the thing this was trying to avoid but is not itself a risk.
    """
    if not WEEKEND_FLAT:
        return None
    now = now or datetime.now(timezone.utc)
    import prop_guard
    if prop_guard.broker_now(now).weekday() != 4:      # 4 = Friday
        return None
    # Same deadline as roll-flat: earliest session close among the covered
    # instruments, capped by the roll. Taking the min means an instrument with a
    # shorter Friday session cannot silently miss its window.
    ends, have_schedule = [], False
    for inst in sorted(WEEKEND_FLAT_INSTS):
        ad = adapters['fix'].get(inst) if adapters else None
        ivs = _session_intervals(inst, ad)
        if ivs:
            have_schedule = True
            e = session_end(now, ivs)
            if e:
                ends.append(e)
    due, day = roll_flat_due(now, _read_weekend_flat_latch(),
                             min(ends) if ends else None, have_schedule)
    if not due:
        return None
    closed, failed = flatten_all(state, adapters, live,
                                 f'weekend-flat {day}', only=WEEKEND_FLAT_INSTS,
                                 tag='weekend-flat', preserve_signal=True)
    if failed:
        print(f"  [weekend-flat] {len(failed)} not closed — retrying while the "
              f"window lasts; each miss carries one weekend of swap")
        return closed, failed
    if live:
        try:
            with open(WEEKEND_FLAT_FILE, 'w') as fh:
                json.dump({'day': day, 'closed': closed,
                           'at': now.isoformat()}, fh, indent=1)
        except Exception as exc:
            # A lost latch re-runs the close, which finds nothing open and is a
            # no-op — strictly safer than skipping the close entirely.
            print(f"  [weekend-flat] could not persist latch: {exc}",
                  file=sys.stderr)
    return closed, failed


def weekend_flat_reopen(state, live, now=None):
    """Hand the surrendered sleeves back to the strategy. Returns [sid] or None.

    PURE STATE, NO BROKER CALLS. It does not place the entry — it clears each
    latched sleeve to FLAT(0) so acts_on_signal reads 0 -> sig as a change and the
    ordinary trade loop opens it at whatever the strategy says THIS morning. That
    matters: a sleeve does not get its Friday position back, it gets a fresh
    decision, and if Monday's signal is 0 it simply stays flat.

    FIRES ON A LATER BROKER DAY THAN THE CLOSE, once per latch. The latch carries
    the broker day it was written on (a Friday) and the sleeves that actually
    closed; any pass whose broker day differs consumes it. Two consequences worth
    naming:

      * The 21:15 UTC Friday pass does NOT reopen — same broker day, still Friday.
        That is the shut-market window, and skipping it is the whole reason the
        reopen is a separate step instead of preserve_signal=False.
      * The first pass of the new week reopens whatever hour it lands on. On this
        pod that is 00:15 UTC Monday, about 3.25h after the weekly open at 21:00
        UTC Sunday, so live re-enters LATER than risk_model_sim's --monday-reentry
        models (it fills at the Sunday-stamped bar's open). Any figure quoted from
        that flag is optimistic by that gap.

    ONLY SLEEVES THE CLOSE ACTUALLY CLOSED, and only ones still flat. A sleeve that
    was stopped out later in the week is also FLAT(signal), and clearing that would
    re-enter a position the stop just took off — so the latch's own `closed` list is
    the authority, never a scan of the state. One that already has a pos_id is
    skipped; it got back in on its own and needs nothing.

    THE STATE IS WRITTEN BEFORE THE LATCH IS MARKED, and that order is load-bearing.
    run_once only persists state at the END of a trading pass, so a reopen that ran
    on an ordinary poll lives in memory until then — mark the latch first and a
    restart in between reloads FLAT(signal) from disk against a latch that says
    "reopened", and the sleeve sits out the whole week with nothing reporting it.
    Writing state first makes the failure mode the harmless one: if the latch write
    then fails, the next pass re-clears sleeves that are already FLAT(0).

    IDEMPOTENT. The latch is marked consumed after a successful reopen, so a second
    poll the same day is a no-op.
    """
    if not (WEEKEND_FLAT and WEEKEND_FLAT_REENTRY):
        return None
    now = now or datetime.now(timezone.utc)
    try:
        with open(WEEKEND_FLAT_FILE) as fh:
            latch = json.load(fh)
    except Exception:
        return None
    if latch.get('reopened') or not latch.get('closed'):
        return None
    import prop_guard
    bnow = prop_guard.broker_now(now)
    day = bnow.strftime('%Y-%m-%d')
    if latch.get('day') == day:
        return None
    # A SHUT MARKET IS NEVER DUE — the same clause roll_flat_due needs, for the same
    # reason, and the day check alone does NOT cover it. Broker midnight is 21:00
    # UTC, so the 21:15 UTC Friday poll is already broker SATURDAY and its day label
    # differs from the latch's Friday. Weekday is the honest test: the broker clock
    # is America/New_York + 7h, so broker Monday 00:00 IS the weekly open at 21:00
    # UTC Sunday, and broker Sat/Sun is exactly the closed window. Clearing to
    # FLAT(0) in there would leave a trading pass — a manual trigger, say — free to
    # order into a market that can only reject.
    if bnow.weekday() >= 5:
        return None
    reopened = []
    for sid in latch['closed']:
        st = state.get(sid)
        if not st or st.get('pos_id'):
            continue
        state[sid] = FLAT(0)
        reopened.append(sid)
    print(f"  [weekend-flat] REOPEN ({latch.get('day')} -> {day}): "
          f"{len(reopened)} of {len(latch['closed'])} handed back to the strategy"
          + (f" ({','.join(sorted(reopened))})" if reopened else ""))
    if live:
        try:
            json.dump(state, open(STATE_FILE, 'w'), indent=2)
        except Exception as exc:
            # The latch stays UNCONSUMED, so the next poll retries the whole thing.
            print(f"  [weekend-flat] could not persist the reopened state: {exc} — "
                  f"latch left unconsumed, retrying next poll", file=sys.stderr)
            return reopened
        try:
            latch['reopened'] = True
            latch['reopened_at'] = now.isoformat()
            latch['reopened_sids'] = reopened
            with open(WEEKEND_FLAT_FILE, 'w') as fh:
                json.dump(latch, fh, indent=1)
        except Exception as exc:
            print(f"  [weekend-flat] could not mark the latch consumed: {exc} — "
                  f"the next poll re-clears sleeves that are already FLAT(0)",
                  file=sys.stderr)
    return reopened


def guard_tick(state, adapters, live):
    """Sample equity and halt if a limit is breached. Returns True if halted.

    Anchors on prop_guard's persisted day state so the runner and the monitor
    agree on which equity 'today' is measured from — a separate anchor here would
    drift from the one the alerts are computed against.
    """
    if not GUARD_ENABLED:
        return False
    balance, equity = _guard_equity(adapters)
    if equity is None:
        return False
    try:
        import prop_guard
        # balance rides along so the midnight base can be max(balance, equity)
        # without a second round trip; prop_guard only consults it at the roll.
        m = prop_guard.update(nav=equity, balance=balance)  # advances peak/day-low, persists
        anchor, start = m.get('day_anchor'), m.get('start_nav')
        low = m.get('day_low')
        today = prop_guard._trading_day(datetime.now(timezone.utc))
    except Exception as exc:
        print(f"  [guard] anchor unavailable — GUARD INACTIVE this tick: {exc}", file=sys.stderr)
        return False

    kind = halt_decision(equity, anchor, start, day_low=low)
    if kind is None:
        return False
    # Report the number the halt was judged on, not the current tick — they differ
    # exactly when the dip that triggered this has already partly recovered.
    judged = min(equity, low) if low else equity
    dd = (judged - (start if kind == 'total' else anchor)) / (start if kind == 'total' else anchor)
    if halt_is_active(_read_halt(), today):
        return True                                   # already latched; do not re-flatten
    print(f"  [guard] 🚨 {kind.upper()} LIMIT — equity {equity:,.2f}, dd {dd*100:+.2f}%")
    _write_halt(kind, today, dd, equity)
    flatten_all(state, adapters, live, f'{kind} drawdown {dd*100:+.2f}%')
    return True


def find_orphans(sleeves, state):
    """State entries still holding a position whose sleeve has LEFT the book.

    load_sleeves() returns only status='paper_trading' and run_once() iterates
    that list, so the moment a sleeve is retired its reconcile, software stop and
    close-on-signal all stop running — while cTrader still holds its position.
    Nothing else sweeps `state`, so the position becomes unmanaged AND unstopped.
    This is exactly how the OANDA book stranded two positions (one for 18 days,
    against its own exit signal). The compact deployment DB is built from
    paper_trading only, so *deploying a retirement* is the normal trigger.
    """
    book = {s['sid'] for s in sleeves}
    return [(sid, st) for sid, st in sorted(state.items())
            if st.get('pos_id') and sid not in book]


def sweep_orphans(sleeves, state, live, adapters, open_ids=None):
    """Close orphaned positions (see find_orphans). Never silent: an orphan that
    cannot be closed is re-reported every pass until it is.

    FIX_CLOSE_ORPHANS=0 downgrades this to report-only — the position then stays
    unmanaged, which on a prop account risks the DD limits with no stop running.
    """
    orphans = find_orphans(sleeves, state)
    if not orphans:
        return
    close = os.getenv('FIX_CLOSE_ORPHANS', '1') == '1'
    for sid, st in orphans:
        inst = P._infer_instrument(sid)
        # A "close" here is just an opposite-side MARKET order tagged with the position's
        # PosMaintRptID(721). cTrader does NOT reject one whose 721 matches nothing — it
        # executes the order for what it literally is, an OPEN. There is no REDUCE_ONLY on
        # this venue (the OANDA path gets that guarantee from positionFill=REDUCE_ONLY), so
        # a stale state entry does not fail safe: it opens a new position in the opposite
        # direction. That is how AUS200 4313903 went from a closed 0.05 long to a live 0.10
        # short on 2026-07-27 — one sell from a dangling stop, one from a close aimed at a
        # position that had already gone. open_ids is the broker's own snapshot, so trust it
        # over our record: if the position is gone, drop the entry and send nothing.
        if open_ids is not None and st['pos_id'] not in open_ids:
            print(f"  ✓ ORPHAN {sid:42} {inst:9} pos {st['pos_id']} already closed at the "
                  f"broker — clearing state, no order sent")
            state.pop(sid, None)
            continue
        print(f"  ⚠️ ORPHAN {sid:42} {inst:9} pos {st['pos_id']} "
              f"units={st.get('units', 0):g} side={st.get('side', 0):+d} "
              f"— sleeve is no longer in the book")
        if not live or not close:
            print("     (report-only: %s) — position stays UNMANAGED and UNSTOPPED"
                  % ('dry-run' if not live else 'FIX_CLOSE_ORPHANS=0'))
            continue
        try:
            ad = adapters['fix'].get(inst) if adapters else None
            if ad is None:
                # Never construct a venue adapter here under cTrader: a bare FixAdapter
                # would open a SECOND session to the same account.
                if VENUE == 'ctrader':
                    print("     ❌ no adapter for %s — cannot close, will retry" % inst)
                    continue
                ad = FixAdapter(inst)
            # Cancel first so the close can't orphan the stop, and CHECK the ack — the
            # stop is a standalone opposite-side order, not an attached SL, so one left
            # working behind a closed position is a naked entry that opens an unmanaged
            # position when it triggers. Both signal-close paths already guard this; the
            # sweep discarded the result and did exactly that to AU200 7832089 and NATGAS
            # 7832091 on 2026-07-27. An unconfirmed cancel means try again next pass.
            if st.get('stop_ref') and ad.cancel_stop(st['stop_ref'], st.get('side', 0)) is None:
                raise RuntimeError('stop cancel unconfirmed — not closing behind a live stop')
            ack = ad.close_position(st['pos_id'], st['units'], st['side'])
            # _order returns the ack dict even for a REJECT (the reject handler builds
            # {'ord_status': '8', ...}), so `is None` alone reads a rejected close as a
            # success. That is how AU200 4313903 was reported "closed" on 2026-07-27
            # while the position stayed open AND its state entry — the only record it
            # existed — was dropped. Validate status exactly as both signal-close paths do.
            if ack is None or ack.get('ord_status') in ('8', '4', 'C'):
                raise RuntimeError(f"close rejected/unfilled: {ack.get('reject') if ack else 'no ack'}")
            print(f"     closed {st['pos_id']} — sleeve state cleared")
            state.pop(sid, None)
        except Exception as e:
            # Keep the state entry: it is the only record that this exposure
            # exists, and dropping it would hide the position permanently.
            print(f"     ❌ close FAILED ({e}) — will retry next pass")


def _stop_ok(ref) -> bool:
    """Did place_stop actually attach the stop?

    place_stop returns {'ord_status':'8','reject':...} on failure, which is TRUTHY — so a
    bare `if ref` logged "stop@broker OK" for a REJECTED stop and stored the reject payload
    as stop_ref. Only ord_status '0' means attached.
    """
    return isinstance(ref, dict) and ref.get('ord_status') == '0'


def _write_receipt(started, error=None):
    """Record that a pass ran, and whether it finished.

    Pod logs retain only ~3 hours, so by morning the log is gone and "the pass never ran"
    is indistinguishable from "it ran and blew up". This file is the durable answer, and
    its age is what an external check watches to notice the runner has died.
    """
    try:
        json.dump({'started': started.isoformat() + 'Z',
                   'finished': datetime.utcnow().isoformat() + 'Z',
                   'ok': error is None,
                   'error': error}, open(RECEIPT_FILE, 'w'), indent=2)
    except OSError as exc:                     # a receipt failure must never kill the runner
        print(f"  [receipt] could not write {RECEIPT_FILE}: {exc}")


def _flat_scope_report(label, scope, sleeves):
    """How many LOADED SLEEVES a carry scope actually covers, plus why if zero.

    WHY THIS IS NOT DECORATION. These scopes are matched against the repo's
    canonical instrument id — the OANDA form, 'XAG_USD' — because that is what
    adapters['fix'] is keyed by and what st['inst'] holds; ctrader_exec translates
    to the broker's own name internally. But the cTrader UI and every fill list show
    the BROKER name: XAGUSD, SP500, CUCUSD, DAX40. Setting those instead matches
    NOTHING, and the failure is completely silent — no error, no rejected order,
    just a book that quietly keeps paying the carry the policy was armed to avoid.
    So the count is printed, and a zero-match scope says so loudly with the
    translation it probably meant.
    """
    held = {s['inst'] for s in sleeves}
    covered = sorted(scope & held)
    print(f"  [{label}] covers {len(covered)} of {len(sleeves)} loaded sleeves"
          + (f": {','.join(covered)}" if covered else ""))
    unknown = sorted(scope - held)
    if not unknown:
        return
    # Is each unmatched name the BROKER's spelling of something real?
    hint = {}
    try:
        import json as _j
        syms = _j.load(open(os.path.join(os.path.dirname(__file__),
                                         'ctrader_symbols.json')))['instruments']
        hint = {v['ctrader_name']: k for k, v in syms.items()}
    except Exception:
        pass
    for name in unknown:
        if name in hint:
            print(f"  [{label}] ⚠️  '{name}' is the BROKER symbol name — this scope "
                  f"takes the canonical id. Did you mean '{hint[name]}'?",
                  file=sys.stderr)
        else:
            print(f"  [{label}] ⚠️  '{name}' matches no loaded sleeve — it is either "
                  f"not in the book or misspelled", file=sys.stderr)
    if not covered:
        print(f"  [{label}] ⚠️⚠️  SCOPE MATCHES NOTHING — the policy is armed but "
              f"will never act, and the carry it exists to avoid is still being "
              f"paid in full", file=sys.stderr)


def _run_triggered(sleeves, state, live, adapters):
    """RUNNER_MODE=cron: never initiate a pass, wait to be asked.

    Deliberately has no clock of its own — that is the whole point. Boot places no orders,
    so a redeploy is no longer a trading action.
    """
    print(f"RUNNER_MODE=cron — no pass on boot, no internal schedule; "
          f"waiting on {TRIGGER_FILE} (poll {TRIGGER_POLL}s)")
    last_recon = None
    ticks = 0
    if GUARD_ENABLED:
        print(f"  [guard] ARMED — daily {GUARD_DAILY_LIM*100:.0f}% / total {GUARD_TOTAL_LIM*100:.0f}%, "
              f"halting at {GUARD_FRACTION*100:.0f}% of a limit "
              f"({GUARD_DAILY_LIM*GUARD_FRACTION*100:.2f}% daily), sampling every "
              f"{GUARD_EVERY*TRIGGER_POLL}s")
    else:
        print("  [guard] not armed (PROP_GUARD_HALT unset) — monitoring only")
    # Say so at boot, for the same reason the guard does: from outside, an armed
    # policy and an unset env var look identical, and this one only acts for ten
    # minutes a day — so its silence is indistinguishable from it being absent
    # until the carry shows up on a statement weeks later.
    if ROLL_FLAT:
        import prop_guard as _pg
        _now = datetime.now(timezone.utc)
        print(f"  [roll-flat] ARMED — closing {','.join(sorted(ROLL_FLAT_INSTS))} "
              f"({'from env' if _RF_FROM_ENV else 'CODE DEFAULT — not set on this host'}) in the "
              f"{ROLL_FLAT_LEAD} min before the broker's midnight "
              f"(broker clock now {_pg.broker_now(_now):%Y-%m-%d %H:%M}, "
              f"day {_pg._trading_day(_now)}); reopen is the next ordinary pass")
        _flat_scope_report('roll-flat', ROLL_FLAT_INSTS, sleeves)
    else:
        print("  [roll-flat] not armed (ROLL_FLAT unset) — carry is paid in full")
    if WEEKEND_FLAT:
        import prop_guard as _pg
        _now = datetime.now(timezone.utc)
        print(f"  [weekend-flat] ARMED — closing "
              f"{','.join(sorted(WEEKEND_FLAT_INSTS))} "
              f"({'from env' if _WF_FROM_ENV else 'CODE DEFAULT — not set on this host'})"
              f" in the {ROLL_FLAT_LEAD} min "
              f"before the FRIDAY close (broker clock now "
              f"{_pg.broker_now(_now):%Y-%m-%d %H:%M}, weekday "
              f"{_pg.broker_now(_now):%a}); "
              + ("REOPEN at the first pass of the next broker week — each sleeve "
                 "gets a fresh decision, not its Friday position back"
                 if WEEKEND_FLAT_REENTRY else
                 "NO reopen (WEEKEND_FLAT_REENTRY=0) — each sleeve waits for a "
                 "genuine signal flip"))
        # READ THE WHOLE LATCH, NOT JUST ITS DAY. Reporting "PENDING" off the day
        # alone says pending for a latch that was consumed days ago — the same shape
        # of lie as the close path's old "still stopped", which is the reason that
        # path now reports the stop's real fate.
        try:
            with open(WEEKEND_FLAT_FILE) as _fh:
                _wf = json.load(_fh)
        except Exception:
            _wf = None
        if _wf:
            if not WEEKEND_FLAT_REENTRY:
                _state = 'DISABLED (WEEKEND_FLAT_REENTRY=0) — those sleeves wait for a flip'
            elif _wf.get('reopened'):
                _state = (f"already consumed at {_wf.get('reopened_at', '?')} "
                          f"({len(_wf.get('reopened_sids') or [])} reopened)")
            else:
                _state = 'PENDING at the next differing broker day'
            print(f"  [weekend-flat] latch on disk: closed "
                  f"{','.join(_wf.get('closed') or []) or 'nothing'} on "
                  f"{_wf.get('day')} — reopen {_state}")
        _flat_scope_report('weekend-flat', WEEKEND_FLAT_INSTS, sleeves)
        overlap = WEEKEND_FLAT_INSTS & ROLL_FLAT_INSTS if ROLL_FLAT else set()
        if overlap:
            print(f"  [weekend-flat] note: also in the roll-flat set "
                  f"({','.join(sorted(overlap))}) — on Fridays weekend-flat wins "
                  f"and those stay out; roll-flat closes them the other four days")
    else:
        print("  [weekend-flat] not armed (WEEKEND_FLAT unset) — weekend carry is "
              "paid in full")
    while True:
        # Sample BETWEEN passes: this is the only thing awake while positions are
        # open, and a daily breach happens intraday, not at the trigger.
        ticks += 1
        if GUARD_ENABLED and ticks % GUARD_EVERY == 0:
            try:
                guard_tick(state, adapters, live)
            except Exception as exc:
                print(f"  [guard] tick failed: {exc}", file=sys.stderr)
        # The pre-roll close rides THIS loop rather than a second cron line. The
        # loop is already awake, already reads the broker clock through
        # prop_guard, and the host cron is +08 with no CRON_TZ support — a
        # scheduled UTC time there is exactly the trap that once fired a trading
        # pass inside the index session close.
        # WEEKEND-FLAT RUNS FIRST, and the order is load-bearing whenever the two
        # sets overlap: on a Friday the weekend close must win, because it writes
        # FLAT(signal) and stays out, whereas roll-flat writes FLAT(0) and would
        # re-establish on the next pass — into a market shut until Sunday. Running
        # it first means roll-flat finds nothing open for a shared instrument.
        if WEEKEND_FLAT:
            try:
                weekend_flat_close(state, adapters, live)
            except Exception as exc:
                print(f"  [weekend-flat] close failed: {exc}", file=sys.stderr)
            # THE REOPEN RUNS AFTER THE CLOSE, and the order costs nothing because
            # the two can never both fire: the close only fires on a broker Friday
            # and the reopen only when the broker day differs from the latch's. It
            # sits here rather than inside the trigger block so the state is
            # already FLAT(0) when the pass reads it — placing no orders itself,
            # it is safe on every poll, not just a trading one.
            try:
                weekend_flat_reopen(state, live)
            except Exception as exc:
                print(f"  [weekend-flat] reopen failed: {exc}", file=sys.stderr)
        if ROLL_FLAT:
            try:
                roll_flat_close(state, adapters, live)
            except Exception as exc:
                print(f"  [roll-flat] close failed: {exc}", file=sys.stderr)
        if os.path.exists(TRIGGER_FILE):
            today = datetime.utcnow().date()
            if live and today != last_recon:
                maybe_reconcile(adapters); last_recon = today
            # CONSUME FIRST. If the pass dies halfway through, the trigger must not still be
            # sitting there to re-fire on the next poll and re-enter everything.
            try:
                os.remove(TRIGGER_FILE)
            except OSError as exc:
                print(f"  [trigger] could not consume {TRIGGER_FILE}: {exc} — skipping")
                time.sleep(TRIGGER_POLL); continue
            started = datetime.utcnow()
            try:
                run_once(sleeves, state, live, adapters, trade=True)
                _write_receipt(started)
            except Exception as exc:
                # Survive a bad pass: the trigger is already consumed so nothing re-runs,
                # and the receipt carries the reason.
                _write_receipt(started, repr(exc))
                print(f"  PASS FAILED: {exc!r}")
        time.sleep(TRIGGER_POLL)


def run_once(sleeves, state, live, adapters, trade=True):
    """trade=True: full pass (reconcile + stops + open/close on signal change).
    trade=False: stop-only backstop (reconcile + software stop, NO entries) — used
    for the hourly safety net between daily-close evals when --at is set."""
    _refresh_marks(adapters)                       # feed live px + quote->USD so equity() is USD-correct
    equity = adapters['equity']() if adapters else FIX_START_EQUITY
    open_ids = {}
    if adapters:                                   # fresh broker snapshot for reconciliation
        try: open_ids = next(iter(adapters['fix'].values())).open_pos_ids()
        except Exception as e: print(f"  [reconcile] failed: {e}")
    print(f"[{datetime.utcnow().isoformat()}] {'LIVE' if live else 'DRY-RUN'}  equity={equity:.2f}  "
          f"sleeves={len(sleeves)}  broker_positions={len(open_ids)}")
    # A latched halt must block the PASS, not just new ticks. Without this the
    # 21:15 trigger would re-open the whole book minutes after the guard flattened
    # it — the breaker would look like it fired and changed nothing.
    if GUARD_ENABLED and trade:
        try:
            import prop_guard
            today = prop_guard._trading_day(datetime.now(timezone.utc))
            halt = _read_halt()
            if halt_is_active(halt, today):
                print(f"  HALTED ({halt.get('kind')} @ {halt.get('dd', 0)*100:+.2f}% on "
                      f"{halt.get('day')}) — no entries this pass."
                      + ("  Clear trading_halt.json to resume." if halt.get('kind') == 'total' else ""))
                trade = False              # reconcile + software stops still run
        except Exception as exc:
            print(f"  [guard] halt check failed, trading normally: {exc}", file=sys.stderr)

    # Before trading: nothing else ever revisits a departed sleeve's position.
    sweep_orphans(sleeves, state, live, adapters, open_ids)
    for s in sleeves:
        sid, inst = s['sid'], s['inst']
        try:                               # per-sleeve isolation: one bad sleeve can't abort the book
            sig, close, atr, kelly = latest(s)
            st = state.get(sid, FLAT())
            ad = adapters['fix'].get(inst) if adapters else None
            min_units, min_spec, min_implied = min_lot_implied_risk(s, atr, equity)

            # (1) RECONCILE: broker no longer holds our position -> broker stop fired or manual close.
            if live and st.get('pos_id') and st['pos_id'] not in open_ids:
                # Cancel the protective stop too: if the stop FIRED it's already filled (cancel is a
                # harmless no-op); if the position was closed manually/by reset, the stop is still
                # pending and would orphan -> a later trigger opens an unwanted hedge.
                if st.get('stop_ref'):
                    ad.cancel_stop(st['stop_ref'], st['side'])
                print(f"  {sid:42} {inst:9} position {st['pos_id']} gone at broker (stop fired / manual) — flat, stop cancelled")
                state[sid] = FLAT(st['signal']); continue

            # (2) SOFTWARE STOP backstop (covers a broker stop that didn't attach). Cancel the
            #     broker stop first so it can't orphan, then close via 721.
            if st.get('pos_id') and st.get('stop'):
                pad = adapters['price'].get(inst) if adapters else None
                px = (pad.get_current_price() if pad else None) or close
                if (st['side'] > 0 and px <= st['stop']) or (st['side'] < 0 and px >= st['stop']):
                    print(f"  {sid:42} {inst:9} 🛑 SOFT STOP @ {px:g} (stop {st['stop']:g}) — close {st['pos_id']}")
                    if live:
                        stop_ref = st.get('stop_ref')
                        if stop_ref and ad.cancel_stop(stop_ref, st['side']) is None:
                            print(f"  {sid:42} {inst:9} stop cancel unconfirmed — keeping broker state")
                            continue
                        close_ack = ad.close_position(st['pos_id'], st['units'], st['side'])
                        if close_ack is None or close_ack.get('ord_status') in ('8', '4', 'C'):
                            print(f"  {sid:42} {inst:9} soft-stop close failed — keeping broker state")
                            continue
                    state[sid] = FLAT(st['signal']); continue

            # (2b) BROKER-STOP RETRY: position tracked but broker stop never attached (rejected/
            #      timed out) -> retry every pass and log the reason. Self-heals software-stop-only
            #      positions once the reject cause is fixed; runs on stop-only passes too.
            if live and ad and st.get('pos_id') and st.get('side') and not st.get('stop_ref'):
                pad = adapters['price'].get(inst) if adapters else None
                ref_px = pad.get_current_price() if pad else None
                sm = s['params'].get('stop_mult', DEFAULT_STOP_MULT)
                stop_px = (ref_px - st['side'] * sm * atr) if ref_px else st.get('stop')
                if stop_px:
                    ref = ad.place_stop(st['pos_id'], st['units'], st['side'], stop_px)
                    if ref:
                        st['stop'] = stop_px; st['stop_ref'] = ref; state[sid] = st
                        print(f"  {sid:42} {inst:9} ✓ broker stop attached on retry @ {stop_px:g}")

            # (3) SIGNAL CHANGE -> close old (cancel stop first) + open new (+ broker stop)
            if not trade:                      # stop-only backstop pass: no entries/exits on signal
                continue
            if not acts_on_signal(sig, st):
                continue
            corr_scale = _corr_scale(s, state)
            units, spec = size_units(s, atr, equity, kelly, corr_scale=corr_scale)
            stop_mult = s['params'].get('stop_mult', DEFAULT_STOP_MULT)
            action = []
            if sig != 0 and min_implied > MAXRISK:
                action.append(f"SKIP OPEN — min-lot {min_units:g}u = {min_implied*100:.1f}% risk > {MAXRISK*100:.0f}% cap (retried next pass)")
                # Keep the state EXACTLY as it was. Writing FLAT(sig) here did two
                # harmful things: it advanced the recorded signal, so the skip was
                # never re-evaluated even once equity grew enough to afford the lot;
                # and because this branch runs BEFORE the close block below, it also
                # dropped a live pos_id while the broker still held the position,
                # leaving a position the runner could never close. sweep_orphans
                # iterates STATE, not the broker book, so that loss is permanent.
                state[sid] = st
                print(f"  {sid:42} {inst:9} sig {st['signal']:+d}->{sig:+d}  {'; '.join(action)}")
                continue
            close_ack = None
            if st['pos_id']:
                action.append(f"CLOSE {st['pos_id']} {st['units']:g}u")
                if live:
                    stop_ref = st.get('stop_ref')
                    if stop_ref and ad.cancel_stop(stop_ref, st['side']) is None:
                        action.append("⚠️ STOP CANCEL UNCONFIRMED — keeping broker state")
                        state[sid] = st
                        print(f"  {sid:42} {inst:9} sig {st['signal']:+d}->{sig:+d}  {'; '.join(action)}")
                        continue
                    close_ack = ad.close_position(st['pos_id'], st['units'], st['side'])
                    if close_ack is None or close_ack.get('ord_status') in ('8', '4', 'C'):
                        action.append("⚠️ CLOSE FAILED — keeping broker state")
                        state[sid] = st
                        print(f"  {sid:42} {inst:9} sig {st['signal']:+d}->{sig:+d}  {'; '.join(action)}")
                        continue
            new = FLAT(sig)
            if sig != 0:
                implied = units * stop_mult * atr * q2usd(inst) / max(equity, 1e-9)
                if implied > MAXRISK:                  # broker min-lot > risk cap -> refuse
                    action.append(f"SKIP OPEN — min-lot {units:g}u = {implied*100:.1f}% risk > {MAXRISK*100:.0f}% cap (retried next pass)")
                    # Any close above has already happened, so pos_id must clear —
                    # but the SIGNAL must not advance, or this open is never retried.
                    new = FLAT(st['signal'])
                else:
                    # stop from the LIVE entry price, not yesterday's daily close — a stale close
                    # can put the stop on the wrong side of current market -> broker rejects it.
                    pad = adapters['price'].get(inst) if adapters else None
                    entry_ref = (pad.get_current_price() if pad else None) or close
                    stop_px = entry_ref - sig * stop_mult * atr
                    # ROLL-FLAT REOPEN: resume the SAME trade, do not start a new one.
                    # The carry is honoured only when the direction is unchanged (a
                    # genuine flip IS a new trade) and only for one broker day, so a
                    # carry stranded by a sleeve that errored out cannot be applied
                    # to something else later.
                    verdict, cs, cu = roll_flat_resume(st, sig, entry_ref)
                    if verdict == 'stopped':
                        action.append(f"ROLL-FLAT STOP-OUT — price {entry_ref:g} "
                                      f"passed the carried stop {cs:g} while flat; "
                                      f"not reopening")
                        state[sid] = FLAT(sig)
                        print(f"  {sid:42} {inst:9} sig {st['signal']:+d}->{sig:+d}  {'; '.join(action)}")
                        continue
                    if verdict == 'resume':
                        stop_px, units = cs, cu
                        action.append(f"roll-flat resume (carried stop {cs:g}, {cu:g}u)")
                    action.append(f"OPEN {'BUY' if sig>0 else 'SELL'} {units:g}u ({implied*100:.2f}% risk) @~{entry_ref:g} stop@{stop_px:g} k={kelly} corr={corr_scale:.1f} decay={s.get('decay_kelly_scale', 1.0):.1f}")
                    if live:
                        pid = ad.execute_order(sig*units, f'fix_{sid}')
                        if pid is None:
                            # The reject may have taught us the broker's REAL min volume. Resize,
                            # then RE-CHECK the risk cap before retrying — a bigger min must SKIP,
                            # never silently open an oversized position.
                            units2, _ = size_units(s, atr, equity, kelly)
                            if units2 != units:
                                implied2 = units2 * stop_mult * atr * q2usd(inst) / max(equity, 1e-9)
                                if implied2 > MAXRISK:
                                    action.append(f"SKIP OPEN — learned min {units2:g}u = "
                                                  f"{implied2*100:.1f}% risk > {MAXRISK*100:.0f}% cap")
                                else:
                                    action.append(f"retry @ learned min {units2:g}u ({implied2*100:.2f}% risk)")
                                    pid = ad.execute_order(sig*units2, f'fix_{sid}')
                                    if pid is not None:
                                        units = units2
                        if pid is None:
                            action.append("⚠️ ENTRY FAILED — no fill / no position (retried next pass)")
                            # Do NOT advance the recorded signal. Writing FLAT(sig)
                            # here marked the sleeve as already-acted-on, so the next
                            # pass compared sig to itself, found no change, and sat
                            # flat — for a daily sleeve that is weeks, until the
                            # signal happens to flip. Observed 2026-07-28: nas100usd
                            # i9's entry was rejected because the pass ran inside the
                            # index close, and the sleeve was then stuck flat with a
                            # live signal it could never act on.
                            # Any close above has already happened, so pos_id clears;
                            # only the signal is preserved.
                            new = FLAT(st['signal'])
                        else:
                            # track the position BEFORE placing the stop: if place_stop throws
                            # or is rejected, reconcile + software-stop still cover it (no orphan).
                            new = {'signal': sig, 'pos_id': pid, 'units': units, 'side': sig, 'stop': stop_px, 'stop_ref': None}
                            state[sid] = new
                            ref = ad.place_stop(pid, units, sig, stop_px)   # BROKER-side protective stop
                            if not _stop_ok(ref):
                                # Retry HERE rather than leaving it to a later pass. The hourly
                                # backstop that used to catch a failed attach does not run under
                                # RUNNER_MODE=cron, so an unprotected position would otherwise
                                # sit that way until the next daily pass.
                                ref = ad.place_stop(pid, units, sig, stop_px)
                            if _stop_ok(ref):
                                new['stop_ref'] = ref
                                action.append("stop@broker OK")
                            else:
                                # Leave stop_ref None, NOT the reject payload: a dict carrying
                                # neither 'order_id' nor 'ref' makes cancel_stop return None and
                                # the runner then refuses to ever close the position. None also
                                # re-arms the attach-retry path above.
                                new['stop_ref'] = None
                                why = ref.get('reject') if isinstance(ref, dict) else ref
                                action.append(f"⚠️ BROKER STOP FAILED — software-stop only ({why})")
                    else:
                        new = {'signal': sig, 'pos_id': 'DRY', 'units': units, 'side': sig, 'stop': stop_px, 'stop_ref': None}
            state[sid] = new
            print(f"  {sid:42} {inst:9} sig {st['signal']:+d}->{sig:+d}  {'; '.join(action)}")
        except Exception as e:
            print(f"  {sid:42} {inst:9} ERROR — sleeve skipped this pass: {e}")
            continue
    if live:
        json.dump(state, open(STATE_FILE,'w'), indent=2)

def _probe_securities(sess, symbols):
    """FIX_PROBE=1: dump every field cTrader returns in SecurityList so we can read the real
    min-volume/step/contract-size per symbol. cTrader only supports SecurityListRequestType=SYMBOL(0),
    so query the no-symbol form (some servers return all) then each mapped symbol id. Read-only."""
    def dump(tag, msgs):
        print(f"[PROBE] {tag}: {len(msgs)} message(s)")
        for mi, pairs in enumerate(msgs):
            print(f"[PROBE]   {tag} msg#{mi} ({len(pairs)} tags): " + " ".join(f"{k}={v}" for k, v in pairs))
    dump("ALL", sess.security_list())                          # no-symbol: maybe returns everything
    for sym in sorted(set(symbols), key=lambda x: int(x) if x.isdigit() else 0):
        msgs = sess.security_list(symbol=sym)
        if msgs:
            dump(f"sym={sym}", msgs)

def maybe_reconcile(adapters):
    """Snap the FIX self-tracked equity to the broker's REAL balance to clear
    swap/commission/rounding drift.

    LEGACY, FIX ONLY. FIX has no NAV, so the balance had to come from a value
    hand-updated from the dashboard (env FIX_BROKER_BALANCE or
    fix_broker_balance.txt). Under VENUE=ctrader — production since 2026-07-27 —
    Open API reports the broker's own equity, so there is nothing to reconcile and
    the variable is no longer used.

    The venue check is FIRST on purpose. It used to sit behind `if not bal`, so
    every production pass printed 'SKIPPED — set FIX_BROKER_BALANCE ... so equity
    can't drift from the real balance' — a warning that reads like the prop DD
    limits are unguarded, for a mechanism that is obsolete on this venue and would
    have been ignored even if the value were set. Fixed 2026-08-09.
    """
    if VENUE == 'ctrader':
        print("  [reconcile] not needed — Open API equity is the broker's own balance")
        return
    bal = os.getenv('FIX_BROKER_BALANCE')
    path = os.path.join(os.path.dirname(__file__), 'fix_broker_balance.txt')
    if bal is None and os.path.exists(path):
        bal = open(path).read().strip()
    if not bal:
        print("  [reconcile] SKIPPED — set FIX_BROKER_BALANCE (or fix_broker_balance.txt) "
              "from the cTrader dashboard so equity can't drift from the real balance")
        return
    try:
        b = float(bal)
        next(iter(adapters['fix'].values())).fix.reconcile(b)
        print(f"  [reconcile] equity snapped to broker balance {b:.2f}")
    except Exception as e:
        print(f"  [reconcile] failed: {e}")

def print_preflight(sleeves, equity, state=None):
    """Returns a process exit code: non-zero when the deployed book would strand
    exposure, so a bad deploy fails loudly instead of orphaning it silently."""
    rc = 0
    orphans = find_orphans(sleeves, state or {})
    if orphans:
        rc = 1
        print(f"❌ {len(orphans)} ORPHANED position(s) — sleeve retired/dropped but "
              f"cTrader still holds it. Nothing in the trading loop will manage or "
              f"stop these; close them before deploying this book:")
        for sid, st in orphans:
            print(f"   {sid:42} {P._infer_instrument(sid):9} pos {st['pos_id']} "
                  f"units={st.get('units', 0):g} side={st.get('side', 0):+d}")
        print()
    print(f"FIX preflight @ equity={equity:.2f}  sleeves={len(sleeves)}  RISK={RISK*100:.2f}%"
          f" x BOOK_SCALE {BOOK_SCALE:g} = EFFECTIVE {EFF_RISK*100:.3f}%  MAXRISK={MAXRISK*100:.2f}%")
    print("-" * 118)
    print(f"{'strategy':42} {'inst':9} {'wtx':>5} {'decay':>5} {'minlot':>10} {'minrisk':>8} {'tradable':>9} reason")
    print("-" * 118)
    for s in sleeves:
        try:
            sig, close, atr, kelly = latest(s)
            min_units, spec, min_implied = min_lot_implied_risk(s, atr, equity)
            tradable = 'YES' if min_implied <= MAXRISK else 'NO'
            reason = '' if tradable == 'YES' else f'min-lot risk {min_implied*100:.2f}% > cap {MAXRISK*100:.2f}%'
            print(f"{s['sid'][:42]:42} {s['inst']:9} {s['ws']:5.2f} {s.get('decay_kelly_scale',1.0):5.1f} {min_units:10g} {min_implied*100:7.2f}% {tradable:>9} {reason}")
        except Exception as e:
            print(f"{s['sid'][:42]:42} {s['inst']:9} {'-':>5} {'-':>5} {'-':>10} {'-':>8} {'ERR':>9} {e}")
    return rc

def _seconds_until(hhmm):
    """Seconds until the next HH:MM UTC (today if still ahead, else tomorrow)."""
    h, m = map(int, hhmm.split(':'))
    now = datetime.utcnow()
    tgt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    return (tgt - now).total_seconds()

def main():
    init_db()
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='place REAL orders (default: dry-run)')
    ap.add_argument('--once', action='store_true', help='one pass then exit')
    ap.add_argument('--preflight', action='store_true', help='print per-sleeve min-lot feasibility report and exit')
    ap.add_argument('--interval', type=int, default=3600, help='poll seconds (used only when --at unset)')
    # ponytail: OANDA daily bar closes 21:00 UTC (US summer) / 22:00 UTC (US winter) — set to
    # a few min after so the new bar is fetchable. DST shifts it, so it's a knob not a constant.
    ap.add_argument('--at', default=os.getenv('FIX_RUN_AT'),
                    help='HH:MM UTC to run once/day (e.g. 21:05). Overrides --interval. Env: FIX_RUN_AT')
    a = ap.parse_args()
    sleeves, skipped = load_sleeves()
    print(f"loaded {len(sleeves)} cTrader-tradeable sleeves; skipped {len(skipped)}: "
          + ", ".join(f'{s}({r})' for s,r in skipped))
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    print(f"RISK/trade={RISK*100:.2f}% x BOOK_SCALE {BOOK_SCALE:g} = EFFECTIVE {EFF_RISK*100:.3f}%  "
          f"MAXRISK={MAXRISK*100:.0f}%  (set BASE_RISK / BOOK_SCALE in .env to change)")
    if a.preflight:
        sys.exit(print_preflight(sleeves, FIX_START_EQUITY, state))
    adapters = None
    if a.live:
        insts = {s['inst'] for s in sleeves}
        price = {i: OandaAdapter(i) for i in insts}                   # OANDA live price for stop checks
        # VENUE=ctrader routes execution over the Open API instead of FIX. Default stays
        # 'fix' so this file is INERT until the flag is set deliberately: rollback is
        # unsetting the env var and restarting, never a code revert.
        # Open API wins on two structural counts — the stop is attached to the position
        # (never a standalone order that can outlive it) and a close is by positionId
        # (a stale one is rejected, not executed as a fresh OPEN, as FIX does).
        if VENUE == 'ctrader':
            import json as _json
            from ctrader_exec import adapter_for as _ct_adapter_for
            from ctrader_client import get_client as _ct_get_client
            _syms = _json.load(open(os.path.join(os.path.dirname(__file__),
                                                 'ctrader_symbols.json')))
            fix = {i: _ct_adapter_for(i, _syms) for i in insts}       # share one CTraderClient
            # cTrader reports a REAL balance; FIX had no NAV, so equity was self-tracked
            # from a manual start figure plus fills and drifted until reconciled by hand.
            _cl = _ct_get_client()
            adapters = {'fix': fix, 'price': price,
                        'equity': lambda: _cl.get_trader()['balance']}
            print(f"VENUE=ctrader — execution over Open API, stops attached to positions")
        else:
            os.environ['BROKER'] = 'fix'
            fix = {i: FixAdapter(i) for i in insts}                   # share one _FixSession
            adapters = {'fix': fix, 'price': price,
                        'equity': next(iter(fix.values())).fix.equity}
            if os.getenv('FIX_PROBE'):                 # read-only: dump cTrader's per-symbol specs
                _probe_securities(next(iter(fix.values())).fix, [ad.symbol for ad in fix.values()])
    # An explicit one-shot is always a FULL pass, whatever RUNNER_MODE says — it is a
    # deliberate human or cron ask, and it is how a deploy gets verified.
    if a.once:
        if a.live:
            maybe_reconcile(adapters)
        run_once(sleeves, state, a.live, adapters, trade=True)
        return

    # RUNNER_MODE=cron removes BOTH ways this process could start a pass by itself: the
    # boot pass (`first`) and the daily `--at` branch below. Unset keeps the old behaviour
    # exactly, so rollback is unsetting the env var, never a code revert.
    if RUNNER_MODE == 'cron':
        _run_triggered(sleeves, state, a.live, adapters)
        return

    STOP_POLL = min(a.interval, 3600)                 # hourly stop-backstop cadence when --at set
    last_recon = None
    first = True
    while True:
        today = datetime.utcnow().date()
        if a.live and today != last_recon:            # once per day, before trading
            maybe_reconcile(adapters); last_recon = today
        if not a.at:                                  # no schedule -> old behavior: trade every poll
            run_once(sleeves, state, a.live, adapters, trade=True)
            if a.once: break
            time.sleep(a.interval); continue
        secs = _seconds_until(a.at)
        if first or secs <= STOP_POLL:                # startup, or the daily close is within reach
            if not first:
                print(f"  {secs/3600:.2f}h to daily close {a.at} UTC — sleeping")
                time.sleep(secs)
            run_once(sleeves, state, a.live, adapters, trade=True)   # FULL: entries/exits
            first = False
            if a.once: break
        else:                                         # between evals: stop backstop only
            run_once(sleeves, state, a.live, adapters, trade=False)
            if a.once: break
            print(f"  stop-check only; trade at {a.at} UTC (~{secs/3600:.1f}h) — recheck {STOP_POLL/3600:.1f}h")
            time.sleep(STOP_POLL)

if __name__ == '__main__':
    main()
