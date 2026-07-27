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
from datetime import datetime, timedelta
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validator import create_strategy_function
from pipeline_utils import init_db
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data
import portfolio as P
from fix_adapter import FixAdapter, _FIX_SYMBOL_ID, FIX_START_EQUITY, _MIN_VOL
from ctrader_adapter import OandaAdapter          # OANDA is the DATA source (price/candles)

RISK = float(os.getenv('FIX_RISK', '0.005'))      # separate from OANDA's RISK_PER_TRADE
MAXRISK = float(os.getenv('FIX_MAXRISK', '0.02'))   # per-trade hard cap; skip open if min-lot exceeds it
DEFAULT_STOP_MULT = 2.0
KELLY_WIN, KELLY_MIN, KELLY_UP = 60, 30, 2.0          # eval mode: 2x on positive edge
STATE_FILE = os.path.join(os.path.dirname(__file__), 'fix_runner_state.json')

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
def round_vol(units, inst):
    """VOL_SPEC is the hand-maintained fallback; a min volume LEARNED from a broker reject
    (fix_adapter._MIN_VOL) always wins, so sizing self-corrects per instrument. NOTE this fixes
    the min/step only — it cannot detect a wrong units BASIS (if 1 FIX unit isn't 1 price-unit,
    risk would be mis-scaled); confirm that from the first fill's accepted qty vs the lot size."""
    mn, st = VOL_SPEC.get(inst, (1, 1))
    learned = _MIN_VOL.get(_FIX_SYMBOL_ID.get(inst))
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
    a = raw[raw != 0][-KELLY_WIN:]
    if len(a) < KELLY_MIN: return 0.5
    w, l = a[a > 0], a[a < 0]
    if len(w) == 0 or len(l) == 0: return 1.0 if len(w) else 0.5
    wr = len(w)/len(a); B = w.mean()/abs(l.mean()); k = wr - (1-wr)/B if B > 0 else 0
    return KELLY_UP if k > 0 else 0.5

def load_sleeves():
    """paper_trading sleeves whose instrument cTrader offers, with portfolio scaling."""
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
    eff = min(RISK * sleeve['ws'] * corr_scale * kelly * sleeve.get('decay_kelly_scale', 1.0), MAXRISK)
    raw = equity * eff / (stop_mult * atr * q2usd(sleeve['inst']))
    return round_vol(raw, sleeve['inst'])              # -> (volume, (min, step))


def min_lot_implied_risk(sleeve, atr, equity):
    stop_mult = sleeve['params'].get('stop_mult', DEFAULT_STOP_MULT)
    units, spec = round_vol(0, sleeve['inst'])
    implied = units * stop_mult * atr * q2usd(sleeve['inst']) / max(equity, 1e-9)
    return units, spec, implied

FLAT = lambda sig=0: {'signal': sig, 'pos_id': None, 'units': 0.0, 'side': 0, 'stop': None, 'stop_ref': None}

def _refresh_marks(adapters):
    """FIX has no quote feed, so equity() would ignore unrealized PnL and book realized in
    quote currency. Feed the session live OANDA prices + quote->USD rates (same convention as
    sizing's q2usd) for each held instrument, so equity reflects USD-converted realized+unrealized.
    Only marks instruments with an open position (bounds the OANDA price calls)."""
    if not adapters: return
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


def sweep_orphans(sleeves, state, live, adapters):
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
        print(f"  ⚠️ ORPHAN {sid:42} {inst:9} pos {st['pos_id']} "
              f"units={st.get('units', 0):g} side={st.get('side', 0):+d} "
              f"— sleeve is no longer in the book")
        if not live or not close:
            print("     (report-only: %s) — position stays UNMANAGED and UNSTOPPED"
                  % ('dry-run' if not live else 'FIX_CLOSE_ORPHANS=0'))
            continue
        try:
            ad = (adapters['fix'].get(inst) if adapters else None) or FixAdapter(inst)
            # Cancel first so the close can't orphan the stop, and CHECK the ack — the
            # stop is a standalone opposite-side order, not an attached SL, so one left
            # working behind a closed position is a naked entry that opens an unmanaged
            # position when it triggers. Both signal-close paths already guard this; the
            # sweep discarded the result and did exactly that to AU200 7832089 and NATGAS
            # 7832091 on 2026-07-27. An unconfirmed cancel means try again next pass.
            if st.get('stop_ref') and ad.cancel_stop(st['stop_ref'], st.get('side', 0)) is None:
                raise RuntimeError('stop cancel unconfirmed — not closing behind a live stop')
            ack = ad.close_position(st['pos_id'], st['units'], st['side'])
            if ack is None:
                raise RuntimeError('close returned no ack')
            print(f"     closed {st['pos_id']} — sleeve state cleared")
            state.pop(sid, None)
        except Exception as e:
            # Keep the state entry: it is the only record that this exposure
            # exists, and dropping it would hide the position permanently.
            print(f"     ❌ close FAILED ({e}) — will retry next pass")


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
    # Before trading: nothing else ever revisits a departed sleeve's position.
    sweep_orphans(sleeves, state, live, adapters)
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
            if sig == st['signal']:
                continue
            corr_scale = _corr_scale(s, state)
            units, spec = size_units(s, atr, equity, kelly, corr_scale=corr_scale)
            stop_mult = s['params'].get('stop_mult', DEFAULT_STOP_MULT)
            action = []
            if sig != 0 and min_implied > MAXRISK:
                action.append(f"SKIP OPEN — min-lot {min_units:g}u = {min_implied*100:.1f}% risk > {MAXRISK*100:.0f}% cap")
                new = FLAT(sig)
                state[sid] = new
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
                    action.append(f"SKIP OPEN — min-lot {units:g}u = {implied*100:.1f}% risk > {MAXRISK*100:.0f}% cap")
                else:
                    # stop from the LIVE entry price, not yesterday's daily close — a stale close
                    # can put the stop on the wrong side of current market -> broker rejects it.
                    pad = adapters['price'].get(inst) if adapters else None
                    entry_ref = (pad.get_current_price() if pad else None) or close
                    stop_px = entry_ref - sig * stop_mult * atr
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
                            action.append("⚠️ ENTRY FAILED — no fill / no position")
                        else:
                            # track the position BEFORE placing the stop: if place_stop throws
                            # or is rejected, reconcile + software-stop still cover it (no orphan).
                            new = {'signal': sig, 'pos_id': pid, 'units': units, 'side': sig, 'stop': stop_px, 'stop_ref': None}
                            state[sid] = new
                            ref = ad.place_stop(pid, units, sig, stop_px)   # BROKER-side protective stop
                            new['stop_ref'] = ref
                            action.append("stop@broker OK" if ref else "⚠️ BROKER STOP FAILED — software-stop only")
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
    swap/commission/rounding drift. FIX has no NAV, so the balance comes from a
    value YOU update from the cTrader/The5ers dashboard: env FIX_BROKER_BALANCE or
    fix_broker_balance.txt. The prop DD limits are judged on the real balance, so
    keeping this current is what stops estimate-drift from hiding a breach."""
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
    print(f"FIX preflight @ equity={equity:.2f}  sleeves={len(sleeves)}  RISK={RISK*100:.2f}%  MAXRISK={MAXRISK*100:.2f}%")
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
    print(f"RISK/trade={RISK*100:.2f}%  MAXRISK={MAXRISK*100:.0f}%  (set FIX_RISK in .env to change)")
    if a.preflight:
        sys.exit(print_preflight(sleeves, FIX_START_EQUITY, state))
    adapters = None
    if a.live:
        os.environ['BROKER'] = 'fix'
        insts = {s['inst'] for s in sleeves}
        fix = {i: FixAdapter(i) for i in insts}                       # share one _FixSession
        price = {i: OandaAdapter(i) for i in insts}                   # OANDA live price for stop checks
        adapters = {'fix': fix, 'price': price, 'equity': next(iter(fix.values())).fix.equity}
        if os.getenv('FIX_PROBE'):                     # read-only: dump cTrader's per-symbol specs
            _probe_securities(next(iter(fix.values())).fix, [ad.symbol for ad in fix.values()])
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
