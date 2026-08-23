"""Control set for the self-critique gate (CRITIQUE_MODELS).

Six cases with a KNOWN correct verdict, covering every rejection category in
_SELF_CRITIQUE_SYSTEM plus the documented over-reject traps (a valid macro
thesis, and a positive `.shift()` that models keep misreading as look-ahead).

    ./venv/bin/python scripts/critique_control.py byteplus:glm-5.2 [more models...]

WHAT THIS CAN AND CANNOT TELL YOU — read before acting on a score.

It answers ONE question: does this candidate OVER-REJECT? That is the failure
mode worth guarding, because an over-rejecting gate starves generation and does
it silently — nothing errors, the batch just quietly produces less. Cases
valid_mean_reversion, valid_macro and positive_shift_trap are the guard; a
candidate that rejects any of them is disqualified.

It does NOT rank models. Measured 2026-08-09: flash/pro/glm all scored 5-6 of 6,
and the single case that separated them (lookahead_sameday) FLIPPED between two
runs minutes apart on the SAME model at temperature 0.0 — provider-side
nondeterminism, not a capability difference. v4-flash went 5/6 then 6/6 and
v4-pro went 6/6 then 5/6. So a 6/6 here is a coin flip, not a qualification, and
n=6 cannot support "model X judges better than Y". Choose the critique head on
INDEPENDENCE (never the THESIS_MODELS head, preferably a different family) and
latency; use this only to confirm the choice is not an over-rejector.

Run it at least twice before believing any per-case result.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
import auto_research as ar

CASES = [
    # (name, expect, instrument, thesis)
    ('valid_mean_reversion', 'pass', 'EUR_USD', {
        'strategy_family': 'mean_reversion', 'timeframe': 'D',
        'rationale': 'Short-horizon overreaction to liquidity shocks reverts within a few '
                     'days; the effect is strongest when the market is ranging rather than '
                     'trending, so the gate confirms a low-efficiency-ratio regime.',
        'entry_condition': 'close < lower Bollinger band (20, 2.0)',
        'filter_condition': 'efficiency_ratio(20) < 0.3',
        'exit_condition': 'close crosses back above the 20-period SMA, or ATR stop at 2.0x',
    }),
    ('valid_macro', 'pass', 'AUD_USD', {
        'strategy_family': 'macro', 'timeframe': 'D',
        'rationale': 'A widening real-yield differential between Australia and the US draws '
                     'capital toward the higher-yielding currency, so the pair trends with '
                     'the policy-rate gap.',
        'entry_condition': 'au_us_real_yield_gap.shift(1) rising over 20 days and > 0',
        'filter_condition': 'atr_percentile(60) between 0.2 and 0.8',
        'exit_condition': 'yield gap stops rising for 5 days, or ATR stop at 2.5x',
    }),
    ('circular_gate', 'reject', 'GBP_USD', {
        'strategy_family': 'trend', 'timeframe': 'D',
        'rationale': 'Trends persist because institutional flow arrives gradually.',
        'entry_condition': 'ADX(14) > 25 and close > SMA(50)',
        'filter_condition': 'ADX(14) > 25',
        'exit_condition': 'close < SMA(50), or ATR stop at 2.0x',
    }),
    ('lookahead_sameday', 'reject', 'AU200_AUD', {
        'strategy_family': 'macro', 'timeframe': 'D',
        'rationale': 'US real yields drive the Australian index.',
        'entry_condition': 'today us_real_yield (published after the Australian close) < '
                           'yesterday us_real_yield',
        'filter_condition': 'realized_vol(20) below its median',
        'exit_condition': 'after 5 bars, or ATR stop at 2.0x',
    }),
    ('fidelity_contradiction', 'reject', 'XAU_USD', {
        'strategy_family': 'mean_reversion', 'timeframe': 'D',
        'rationale': 'Price REVERTS after an extended move, so we fade the extreme.',
        'entry_condition': 'close breaks above the 20-day high (buy the breakout)',
        'filter_condition': 'autocorr(10) < 0',
        'exit_condition': 'trailing stop at 2.0x ATR',
    }),
    # Documented stubborn over-reject: a POSITIVE shift is a PAST value.
    ('positive_shift_trap', 'pass', 'USD_JPY', {
        'strategy_family': 'momentum', 'timeframe': 'D',
        'rationale': 'The 12-month trend excluding the most recent month carries a premium, '
                     'because the last month is contaminated by short-term reversal.',
        'entry_condition': 'close.shift(21) / close.shift(252) - 1 > 0',
        'filter_condition': 'realized_vol(60) < its 80th percentile',
        'exit_condition': 'the 12-1 return turns negative, or ATR stop at 3.0x',
    }),
]

def main(candidates):
    """Guarded so importing CASES elsewhere does not fire 18 live LLM calls."""
    for model in candidates:
        ar.SELF_CRITIQUE_MODELS = [model]
        hits, over_rejects, fail_opens, rows = 0, [], [], []
        for name, expect, inst, th in CASES:
            r = ar.self_critique_thesis(th, inst)
            got = r['verdict']
            ok = (got == expect)
            hits += ok
            # The only disqualifying outcome: rejecting a thesis that is fine.
            if expect == 'pass' and got == 'reject':
                over_rejects.append(name)
            # Prefer the explicit flag; the string check is the old fallback.
            failopen = bool(r.get('failed_open')) or 'fail-open' in r.get('reason', '')
            if failopen:
                fail_opens.append(name)
            rows.append((name, expect, got, 'OK' if ok else 'MISS',
                         ('  [FAIL-OPEN] ' if failopen else '  ') + r.get('reason', '')[:95]))
        print(f"\n=== {model}  ->  {hits}/{len(CASES)}")
        for n, e, g, ok, why in rows:
            print(f"  {ok:4} {n:24} expect={e:6} got={g:6} {why}")
        # This verdict is the point of the script; the score above is not.
        #
        # FAIL-OPENS ARE CHECKED FIRST, and this ordering is load-bearing. The
        # gate fails open, so a model that is simply DEAD (2026-08-22:
        # qwen3.7-flash returned HTTP 404 "Model not exist." on all 12 calls)
        # passes every case and records ZERO over-rejections — and this script
        # used to bless it as "usable". No judging happened at all. A run with
        # any fail-open is not evidence of anything.
        if fail_opens:
            print(f"  VERDICT: UNQUALIFIED — {len(fail_opens)} of {len(CASES)} cases FAILED OPEN "
                  f"({', '.join(fail_opens)}). The model never judged them; this run proves nothing. "
                  f"Check the model id is served before reading any score above.")
        elif over_rejects:
            print(f"  VERDICT: DISQUALIFIED — over-rejects {', '.join(over_rejects)}")
        else:
            print("  VERDICT: usable (no over-rejection). Score does NOT rank models — see docstring.")


if __name__ == '__main__':
    main(sys.argv[1:] or ['byteplus:glm-5.2'])
