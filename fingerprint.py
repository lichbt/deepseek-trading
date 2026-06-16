"""Instrument fingerprint — measured in-sample statistical structure.

Direction A of "make the LLM design strategies instead of recall them"
(2026-06-16). The thesis generator otherwise interpolates published strategies
from its pretraining (all widely known → mostly arbitraged). Feeding it the
*measured* structure of each instrument — computed from the in-sample window it
will be optimised on — forces it to reason about THIS market rather than recall
a textbook pattern, and lets us reject theses whose assumed behaviour
contradicts what the data actually shows (e.g. mean-reversion on a positively
autocorrelated series).

Computed on the dev (in-sample) window ONLY — never the holdout — so designing
against it doesn't leak out-of-sample information; the WF/holdout gates still
judge generalisation. Everything is best-effort and FAILS SOFT: any error
returns None and the caller proceeds exactly as before (no fingerprint).
"""
from functools import lru_cache
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

# Strong-enough unconditional autocorrelation that a thesis assuming the
# OPPOSITE behaviour is contradicted even after regime gating. Daily |ac1| for FX
# /indices is usually < 0.05, so 0.06 only fires on a clear, exploitable signal —
# conservative by design (mirrors the self-critique "reject only on a clear flaw"
# stance). Tunable.
AC_CONTRADICTION = 0.06
MIN_BARS = 200


def _t_stat(subset: pd.Series, full: pd.Series) -> float:
    """Rough significance of a subset's mean vs the full series' noise level."""
    if subset is None or len(subset) < 5 or full.std() == 0:
        return 0.0
    return float(subset.mean() / (full.std() / np.sqrt(len(subset))))


@lru_cache(maxsize=256)
def compute_fingerprint(instrument: str, granularity: str = 'D') -> Optional[Dict[str, Any]]:
    """Measured in-sample structure of `instrument` at `granularity`, or None.

    Cached per (instrument, granularity) for the process — the dev window is
    fixed, so the fingerprint is stable within a batch.
    """
    try:
        from validator import get_dev_window
        from data_fetcher import get_candles_date_range
        ds, de = get_dev_window(instrument)
        df = get_candles_date_range(instrument, ds, de, granularity=granularity)
        if df is None or len(df) < MIN_BARS or 'close' not in df.columns:
            return None
        close = pd.Series(np.asarray(df['close'], dtype=float))
        ret = close.pct_change().dropna()
        if len(ret) < MIN_BARS // 2 or ret.std() == 0:
            return None

        def ac(lag: int) -> float:
            return float(ret.autocorr(lag)) if len(ret) > lag + 20 else 0.0

        ac1, ac5, ac20 = ac(1), ac(5), ac(20)
        vol_ac1 = float(ret.abs().autocorr(1)) if len(ret) > 25 else 0.0

        # Kaufman efficiency ratio (20) — trending (→1) vs choppy (→0)
        net = (close - close.shift(20)).abs()
        path = close.diff().abs().rolling(20).sum()
        er = float((net / path.replace(0, np.nan)).dropna().median())

        skew = float(ret.skew())
        kurt = float(ret.kurtosis())  # excess kurtosis

        # Calendar effects, with a significance check so the LLM only sees REAL
        # ones (directly attacks the post-hoc "Monday effect" failure mode).
        cal = _calendar_effects(df, ret)

        fp = {
            'instrument': instrument, 'granularity': granularity,
            'window': (str(ds), str(de)), 'n': int(len(ret)),
            'ac1': round(ac1, 3), 'ac5': round(ac5, 3), 'ac20': round(ac20, 3),
            'vol_ac1': round(vol_ac1, 3), 'er': round(er, 3),
            'skew': round(skew, 2), 'kurt': round(kurt, 1),
            'calendar': cal,
        }
        fp['character'] = _character(fp)
        return fp
    except Exception:
        return None  # fail soft — caller proceeds with no fingerprint


def _calendar_effects(df: pd.DataFrame, ret: pd.Series) -> Dict[str, Any]:
    """Strongest day-of-week and month-end effect IF significant (|t|>2), else none."""
    try:
        dates = pd.to_datetime(df['date']).reset_index(drop=True).iloc[1:]  # align to ret
        r = ret.reset_index(drop=True)
        r.index = dates.values
        out: Dict[str, Any] = {'dow': None, 'monthend': None}
        # day of week
        dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        best_t, best = 0.0, None
        for d, grp in r.groupby(pd.DatetimeIndex(r.index).dayofweek):
            t = _t_stat(grp, r)
            if abs(t) > abs(best_t):
                best_t, best = t, (dow_names[int(d)], float(grp.mean()), t)
        if best and abs(best_t) > 2.0:
            out['dow'] = {'day': best[0], 'mean_bps': round(best[1] * 1e4, 1), 't': round(best[2], 1)}
        # month-end (last 3 trading days of each month)
        idx = pd.DatetimeIndex(r.index)
        is_me = idx.to_series().groupby([idx.year, idx.month]).transform(
            lambda s: s >= s.nlargest(min(3, len(s))).min()).values
        me = r[is_me]
        t_me = _t_stat(me, r)
        if abs(t_me) > 2.0:
            out['monthend'] = {'mean_bps': round(float(me.mean()) * 1e4, 1), 't': round(t_me, 1)}
        return out
    except Exception:
        return {'dow': None, 'monthend': None}


def _character(fp: Dict[str, Any]) -> str:
    """One-phrase summary of the dominant structure."""
    bits = []
    ac1 = fp['ac1']
    if ac1 > AC_CONTRADICTION:
        bits.append('positively autocorrelated (momentum)')
    elif ac1 < -AC_CONTRADICTION:
        bits.append('negatively autocorrelated (mean-reverting)')
    else:
        bits.append('near-zero autocorrelation (no unconditional drift)')
    bits.append('trending' if fp['er'] >= 0.30 else 'choppy/ranging')
    if fp['vol_ac1'] >= 0.10:
        bits.append('persistent vol regimes')
    if fp['kurt'] >= 3.0 or abs(fp['skew']) >= 0.5:
        tail = 'fat-tailed'
        if fp['skew'] <= -0.5:
            tail += ', downside-asymmetric'
        elif fp['skew'] >= 0.5:
            tail += ', upside-asymmetric'
        bits.append(tail)
    cal = fp.get('calendar') or {}
    if cal.get('dow') or cal.get('monthend'):
        bits.append('calendar effect present')
    return '; '.join(bits)


def format_compact(fp: Optional[Dict[str, Any]]) -> str:
    """One-line summary appended to a batch item line (~25 tokens)."""
    if not fp:
        return ''
    cal = fp.get('calendar') or {}
    cal_s = (cal['dow']['day'] if cal.get('dow') else
             ('month-end' if cal.get('monthend') else 'none'))
    return (f"STRUCTURE[ac1 {fp['ac1']:+.2f}, ac5 {fp['ac5']:+.2f}, ER {fp['er']:.2f}"
            f"{'→trending' if fp['er'] >= 0.30 else '→choppy'}, skew {fp['skew']:+.1f}, "
            f"volClust {fp['vol_ac1']:.2f}, cal:{cal_s}]")


def format_full(fp: Optional[Dict[str, Any]]) -> str:
    """Multi-line block for the per-instrument / logging path."""
    if not fp:
        return ''
    cal = fp.get('calendar') or {}
    if cal.get('dow'):
        cal_s = f"{cal['dow']['day']} effect (mean {cal['dow']['mean_bps']:+.1f}bps, t={cal['dow']['t']:+.1f})"
    elif cal.get('monthend'):
        cal_s = f"month-end effect (mean {cal['monthend']['mean_bps']:+.1f}bps, t={cal['monthend']['t']:+.1f})"
    else:
        cal_s = 'no material day-of-week or month-end effect'
    return (
        f"MEASURED STRUCTURE — {fp['instrument']} {fp['granularity']} "
        f"(in-sample {fp['window'][0]}→{fp['window'][1]}, n={fp['n']}):\n"
        f"  return autocorr: lag1 {fp['ac1']:+.2f}, lag5 {fp['ac5']:+.2f}, lag20 {fp['ac20']:+.2f}\n"
        f"  efficiency ratio(20): {fp['er']:.2f} ({'trending' if fp['er'] >= 0.30 else 'choppy/ranging'})\n"
        f"  vol clustering (ACF|r| lag1): {fp['vol_ac1']:.2f}\n"
        f"  daily skew {fp['skew']:+.2f}, excess kurtosis {fp['kurt']:+.1f}\n"
        f"  calendar: {cal_s}\n"
        f"  → character: {fp['character']}"
    )


def contradiction(thesis: Dict[str, Any], fp: Optional[Dict[str, Any]]) -> Optional[str]:
    """Reason string if the thesis's core directional assumption contradicts the
    MEASURED unconditional autocorrelation, else None. Conservative: fires only on
    a strong, clear contradiction (|ac1| >= AC_CONTRADICTION, opposite sign), so a
    regime-gated edge isn't wrongly killed. Fails soft (None) on any error."""
    try:
        if not fp:
            return None
        ac1 = fp.get('ac1', 0.0)
        fam = str(thesis.get('strategy_family', '')).lower()
        text = ' '.join(str(thesis.get(k, '')) for k in
                        ('rationale', 'entry_condition', 'exit_condition')).lower()
        rev_kw = any(w in text for w in ('revert', 'reversion', 'reversal', 'fade',
                     'oversold', 'overbought', 'bounce', 'snap back', 'snapback', 'mean-revert'))
        mom_kw = any(w in text for w in ('trend', 'momentum', 'breakout', 'continuation',
                     'persist', 'carry'))
        assumes_reversion = 'mean_reversion' in fam or 'statistical' in fam or rev_kw
        assumes_momentum = 'trend' in fam or 'regime' in fam or mom_kw

        if assumes_reversion and not assumes_momentum and ac1 >= AC_CONTRADICTION:
            return (f"thesis assumes mean-reversion but {fp['instrument']} {fp['granularity']} "
                    f"returns are positively autocorrelated (lag1 {ac1:+.2f}) — momentum, "
                    f"not reversion, in-sample")
        if assumes_momentum and not assumes_reversion and ac1 <= -AC_CONTRADICTION:
            return (f"thesis assumes trend/momentum but {fp['instrument']} {fp['granularity']} "
                    f"returns are negatively autocorrelated (lag1 {ac1:+.2f}) — mean-reverting, "
                    f"not trending, in-sample")
        return None
    except Exception:
        return None
