"""Control set for the self-critique gate (CRITIQUE_MODELS).

Eight cases with a KNOWN correct verdict, covering every rejection category in
_SELF_CRITIQUE_SYSTEM plus the documented over-reject traps (a valid macro
thesis, a positive `.shift()` that models keep misreading as look-ahead, and —
added 2026-09-16 after a live sample — the two ways Step 3a gets applied too
widely: a gate implied by the mechanism's STORY rather than by the entry's text,
and selling a new high, which is a FADE and not a breakout).

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
    # OVER-REJECT TRAP 3 (added 2026-09-16, from a live sample of the rewritten
    # CREATIVE[2] item). Step 3a compares the filter against the ENTRY CONDITION
    # TEXT. Here the flow window is implied by the RATIONALE's story ("the flow
    # only exists in the last trading day") but NOT by the entry, which is a plain
    # 5-bar low fade that fires any day of the year — so the gate adds real state
    # and the thesis is fine. A head that rejects this is applying 3a to the
    # mechanism instead of to the entry, which is the over-reach that cost 2 of 3
    # live theses and sends the model back to invent a second calendar gate.
    ('gate_implied_by_story_not_entry', 'pass', 'EUR_USD', {
        'strategy_family': 'event-driven', 'timeframe': 'D',
        'rationale': 'Month-end index rebalancing forces mechanical, price-insensitive hedging '
                     'flow into the last trading day of the month, and that pressure '
                     'mean-reverts once the flow window closes.',
        'entry_condition': 'go LONG when close < the prior 5-bar low '
                           '(close.rolling(5).min().shift(1)); go SHORT above the prior 5-bar high',
        'filter_condition': 'turn_of_month == 1',
        'exit_condition': 'close crosses back through the 5-bar mean, or after 5 bars',
    }),
    # OVER-REJECT TRAP 4 (added 2026-09-16). _SELF_CRITIQUE_SYSTEM's own first
    # instruction says selling a NEW HIGH is MEAN-REVERSION and warns that calling
    # a fade a breakout is "the single most common review error" — yet the set had
    # no case guarding it, and a live sample produced a reject whose reason
    # labelled this exact entry "3-bar breakout/breakdown".
    ('sell_new_high_is_a_fade', 'pass', 'XAU_USD', {
        'strategy_family': 'mean_reversion', 'timeframe': 'D',
        'rationale': 'Stop clusters and forced liquidation flow push price past the recent '
                     'extremes; once the mandated selling completes the dislocation reverts.',
        'entry_condition': 'go SHORT when close > the prior 3-bar high '
                           '(close.rolling(3).max().shift(1)); go LONG below the prior 3-bar low',
        'filter_condition': 'ATR(14) above its 50-bar median',
        'exit_condition': 'close reverts to the 3-bar mean, or the bar range exceeds 2.0x ATR(14)',
    }),
    # Added 2026-09-29 from a live sample of 10 rejections: 4 of them were wrong,
    # and they fell into these two shapes. Both expect PASS, so they belong in the
    # over-reject guard rather than in a new bucket.
    ('exit_target_sits_in_the_positions_favour', 'pass', 'BTC_USD', {
        # The judge read `prev_close - 0.5*gap` as "below the entry open, immediate
        # loss" without doing the sign arithmetic: for a gap-DOWN, gap < 0, so that
        # level is prev_close + 0.5*|gap| — above the entry fill and below the prior
        # close, i.e. a 50%-fill profit target.
        'strategy_family': 'mean_reversion', 'timeframe': 'D',
        'rationale': 'Overnight UTC-roll gaps on 24/7 crypto mark thin-book dislocation; '
                     'large gaps are unfilled inventory imbalances that partially revert as '
                     'liquidity returns, so fading an overextended gap earns the fill.',
        'entry_condition': "gap = df['open'] - df['close'].shift(1); gap_atr = gap / ATR(14); "
                           "q = |gap_atr|.rolling(60).quantile(0.8); go LONG when gap_atr <= -q "
                           "(buy the overextended gap-down); go SHORT when gap_atr >= +q",
        'filter_condition': 'abs(close - SMA(50)) / ATR(14) < 2.0, so the gap is a fresh '
                            'dislocation rather than the tail of an existing trend',
        'exit_condition': 'exit at the 50% gap fill, i.e. close.shift(1) - 0.5*gap for a LONG '
                          '(for a gap-down that is ABOVE the entry fill and below the prior '
                          'close), or after 5 bars',
    }),
    ('filter_disjunction_adds_a_vol_state', 'pass', 'EUR_USD', {
        # Step 3a forbids a disjunction the ENTRY already requires. Here the entry
        # requires days_to_event<=2 while the filter's event disjuncts name the
        # POST-release flags, which can both be false while the entry is true, and
        # the third disjunct is a volatility state — the gate can be false with the
        # entry true, so it gates something.
        'strategy_family': 'event-driven', 'timeframe': 'D',
        'rationale': 'Pre-release compression squeezes range extremes as desks flatten risk '
                     'into a known print; the conditioning axis is the volatility state, and '
                     'the post-release window is a secondary place the same dislocation pays.',
        'entry_condition': 'when days_to_event > 0 AND days_to_event <= 2: go SHORT when close '
                           '> the prior 10-bar high; go LONG below the prior 10-bar low',
        'filter_condition': 'event_window == 1 OR days_since_event <= 1 OR realized_vol(20) > '
                            "its 60-bar median — the volatility state is the axis; on a day with "
                            'days_to_event == 2 both event flags are false, so the gate can be '
                            'false while the entry fires',
        'exit_condition': 'close reverts through the mid of the prior 10-bar range, or after 3 bars',
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
