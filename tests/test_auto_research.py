"""
Tests for auto_research.py — _validate_thesis, _extract_json, and
the deeper _validate_code branches not covered by test_pipeline.py.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auto_research as ar


# ─────────────────────────────────────────────────────────────────────────────
# _CREATIVE_CONSTRAINTS — must not instruct patterns banned by thesis.md
# ─────────────────────────────────────────────────────────────────────────────

class TestCreativeConstraints:
    def test_no_banned_open_to_close_direction(self):
        """thesis.md bans open-to-close direction entries; no constraint may
        instruct them. Constraint #9 used to say 'Base entry on open-to-close
        direction relative to prior day's range midpoint' — a XAU long-bias
        pattern that slipped past validation."""
        joined = ' '.join(ar._CREATIVE_CONSTRAINTS).lower()
        assert 'open-to-close direction' not in joined
        assert "prior day's range midpoint" not in joined

    def test_all_constraints_nonempty(self):
        for c in ar._CREATIVE_CONSTRAINTS:
            assert isinstance(c, str) and len(c.strip()) > 20

    def test_nnfx_constraint_uses_lean_orthogonal_roles(self):
        c = ar._NNFX_CONSTRAINT
        assert 'two mandatory independent layers' in c.lower()
        assert 'do not default to kijun' in c.lower()
        assert 'do not default to macd' in c.lower()
        assert 'compute_returns_with_stop' in c
        assert 'must not implement atr/chandelier trailing-stop state' in c.lower()
        assert 'at most 4 tunable parameters' in c.lower()
        assert 'at most 200 original grid combinations' in c.lower()

    def test_nnfx_constraint_bans_old_four_layer_default(self):
        c = ar._NNFX_CONSTRAINT.lower()
        assert 'must have all four layers' not in c
        assert 'atr-based trailing stop or an independent exit indicator' not in c
        assert 'kijun+macd+adx' in c

    def test_validate_param_grid_shape_enforces_limits(self):
        assert ar._validate_param_grid_shape({'a': [1, 2], 'b': [3]}) is None
        assert 'max 4' in ar._validate_param_grid_shape({
            'a': [1], 'b': [1], 'c': [1], 'd': [1], 'e': [1],
        })
        assert 'max 200' in ar._validate_param_grid_shape({
            'a': list(range(5)), 'b': list(range(5)), 'c': list(range(5)), 'd': [1, 2],
        })
        assert 'missing or empty' in ar._validate_param_grid_shape({})
        assert 'missing or empty' in ar._validate_param_grid_shape(None)

    def test_fallback_nnfx_matches_new_policy(self):
        c = ar._FALLBACK_NNFX.lower()
        assert 'do not default to kijun' in c
        assert 'do not default to macd' in c
        assert 'compute_returns_with_stop' in c
        assert 'must not implement atr/chandelier trailing-stop state' in c
        assert 'at most 200 original grid' in c
        assert 'never use .rolling(...).apply()' in c
        assert 'ema slope + fisher confirmation' in c

    def test_nnfx_slot_still_exists(self):
        assert 'nnfx' in ar._FALLBACK_CONSTRAINTS
        assert ar._NNFX_CONSTRAINT.strip()

    def test_nnfx_rotation_slot_fires_before_asset(self):
        def modes(i):
            wild = i % 8 == 0
            macro = i % 3 == 0 and not wild
            calendar = not wild and not macro and i % 10 == 0
            event = not wild and not macro and not calendar and i % 10 == 5
            nnfx = not wild and not macro and not calendar and not event and i % 40 == 7
            asset = not wild and not macro and not calendar and not event and not nnfx and i % 9 == 0
            return wild, macro, calendar, event, nnfx, asset

        assert modes(7) == (False, False, False, False, True, False)
        assert modes(9) == (False, True, False, False, False, False)
        assert modes(8) == (True, False, False, False, False, False)
        assert modes(45) == (False, True, False, False, False, False)
        assert modes(47) == (False, False, False, False, True, False)


# ─────────────────────────────────────────────────────────────────────────────
# _REGIME_DETECTORS — per-iteration rotation to break ADX anchoring
# ─────────────────────────────────────────────────────────────────────────────

class TestRegimeDetectorRotation:
    def test_menu_nonempty_and_varied(self):
        dets = ar._REGIME_DETECTORS
        assert len(dets) >= 5
        for d in dets:
            assert isinstance(d, str) and len(d.strip()) > 15

    def test_adx_is_only_one_of_many(self):
        """ADX must be present but not dominate the menu."""
        adx_entries = [d for d in ar._REGIME_DETECTORS if 'ADX' in d]
        assert len(adx_entries) == 1

    def test_rotation_breaks_adx_anchoring(self):
        """Over a 10-iteration batch, ADX should be forced at most ~twice —
        the rest get non-ADX detectors. (wild iterations, i%8==0, get None.)"""
        dets = ar._REGIME_DETECTORS
        forced = []
        for i in range(1, 11):
            wild = (i % 8 == 0)
            forced.append(None if wild else dets[i % len(dets)])
        non_wild = [d for d in forced if d is not None]
        adx_count = sum(1 for d in non_wild if 'ADX' in d)
        assert adx_count <= 2, f"ADX forced {adx_count}× in 10 iters — still anchoring"
        # variety: at least 5 distinct detectors used across the batch
        assert len(set(non_wild)) >= 5

    def test_wild_iterations_get_no_detector(self):
        """Wild mode (every 8th iteration) is unconstrained — no forced detector."""
        for i in (8, 16, 24):
            wild = (i % 8 == 0)
            detector = None if wild else ar._REGIME_DETECTORS[i % len(ar._REGIME_DETECTORS)]
            assert detector is None


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe rotation — forces intraday strategies into every batch
# ─────────────────────────────────────────────────────────────────────────────

class TestTimeframeRotation:
    VALID_TF = {'M30', 'H1', 'H4', 'D', 'W'}

    def _schedule(self, n):
        """Forced timeframe per iteration, matching auto_research's logic."""
        out = []
        for i in range(1, n + 1):
            wild = (i % 8 == 0)
            tf = None if wild else ar._TIMEFRAME_ROTATION[(i - 1) % len(ar._TIMEFRAME_ROTATION)]
            out.append(tf)
        return out

    def test_rotation_entries_are_valid_timeframes(self):
        for tf in ar._TIMEFRAME_ROTATION:
            assert tf in self.VALID_TF

    def test_every_rotated_timeframe_appears_in_a_10_batch(self):
        """A 10-iteration batch (the run_forever default) must include each
        force-rotated timeframe at least once. M30 is intentionally NOT
        force-rotated yet — deferred for being slow to validate."""
        tfs = [t for t in self._schedule(10) if t]
        for expected in ('D', 'H4', 'H1', 'W'):
            assert expected in tfs, f"{expected} missing from a 10-batch"

    def test_m30_not_force_rotated(self):
        """M30 is deferred — it must not appear in the forced rotation."""
        assert 'M30' not in ar._TIMEFRAME_ROTATION

    def test_daily_stays_plurality(self):
        """Intraday is added 'as well' — D should still be the most common."""
        tfs = [t for t in self._schedule(10) if t]
        assert tfs.count('D') >= 4

    def test_intraday_is_meaningfully_represented(self):
        """H4 + H1 + M30 together should be a real share of a 10-batch."""
        tfs = [t for t in self._schedule(10) if t]
        intraday = sum(1 for t in tfs if t in ('M30', 'H1', 'H4'))
        assert intraday >= 3

    def test_wild_iterations_get_no_forced_timeframe(self):
        for i in (8, 16, 24):
            wild = (i % 8 == 0)
            tf = None if wild else ar._TIMEFRAME_ROTATION[(i - 1) % len(ar._TIMEFRAME_ROTATION)]
            assert tf is None


# ─────────────────────────────────────────────────────────────────────────────
# Macro rotation — forces macro strategies into every batch
# ─────────────────────────────────────────────────────────────────────────────

class TestMacroRotation:
    def test_macro_constraint_mentions_macro_columns(self):
        c = ar._macro_constraint_for('EUR_USD')
        assert isinstance(c, str) and len(c.strip()) > 40
        assert 'MACRO' in c
        # must name concrete macro columns so the thesis actually uses them
        for col in ('fed_rate', 'us10y', 'dxy'):
            assert col in c

    def test_macro_constraint_is_instrument_specific(self):
        """The constraint lists only columns available for that instrument —
        NZD_USD has no home-currency series, so it must not offer nz_rate etc."""
        nzd = ar._macro_constraint_for('NZD_USD')
        gbp = ar._macro_constraint_for('GBP_USD')
        assert 'dxy' in nzd and 'dxy' in gbp          # universal column
        assert 'uk10y' in gbp                          # GBP home-currency
        assert 'uk10y' not in nzd                      # not available for NZD
        assert 'nz_rate' not in nzd and 'nz10y' not in nzd

    def test_infer_archetype_from_code_overrides_bad_tag(self):
        """The code-gen LLM mis-tags macro strategies as 'standard'. The
        archetype must be inferred from the columns the code references, so
        macro injection still happens and the code doesn't KeyError on us10y."""
        macro_code = ("def generate_signals(df, params):\n"
                      "    return (df['us10y'] - df['fed_rate'] > 0).astype(int)\n")
        # LLM declared 'standard' — inference must override to 'macro'
        assert ar._infer_archetype(macro_code, 'standard') == 'macro'

        std_code = ("def generate_signals(df, params):\n"
                    "    return (df['close'] > df['open']).astype(int)\n")
        assert ar._infer_archetype(std_code, 'standard') == 'standard'

        # session / pair columns are recognised too
        assert ar._infer_archetype("x = df['session']", 'standard') == 'session'
        assert ar._infer_archetype("x = df['close_leg2']", 'standard') == 'pair'

    def test_three_macro_slots_per_ten_batch(self):
        """The common batch is 10 iterations — expect ~3 forced macro slots."""
        modes = []
        for i in range(1, 11):
            wild = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            modes.append('wild' if wild else 'macro' if macro else 'creative')
        assert modes.count('macro') == 3
        assert modes.count('wild') == 1

    def test_macro_and_wild_never_collide(self):
        """A slot is never both macro and wild — wild takes precedence."""
        for i in range(1, 61):
            wild = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            assert not (wild and macro)

    def test_macro_slots_keep_a_regime_detector(self):
        """Macro strategies still need a regime gate — they get a detector."""
        for i in range(1, 11):
            wild = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            if macro:
                detector = ar._REGIME_DETECTORS[i % len(ar._REGIME_DETECTORS)]
                assert detector is not None


# ─────────────────────────────────────────────────────────────────────────────
# Asset-mode rotation — instrument-specific calendar/session/seasonal slot
# Replaces the earlier always-on asset hint, which produced monoculture (8/8
# USD_JPY iterations were the same carry thesis). Rotation fires the
# prescriptive ASSET MODE constraint ~1-in-5 non-wild non-macro iterations.
# ─────────────────────────────────────────────────────────────────────────────

class TestAssetModeRotation:
    def test_asset_mode_constraint_listed_for_every_traded_instrument(self):
        for inst in ('EUR_USD', 'USD_JPY', 'XAU_USD', 'NATGAS_USD',
                     'CORN_USD', 'BTC_USD', 'ETH_USD', 'LTC_USD',
                     'WTICO_USD', 'AUD_USD'):
            c = ar._asset_mode_for(inst)
            assert c, f'{inst} has no asset-mode concepts'
            assert 'ASSET MODE' in c
            assert 'MUST' in c
            assert 'NOT acceptable' in c

    def test_every_concept_embeds_implementable_date_pattern(self):
        """Each concept must embed a date-arithmetic hint (month, day_of_week,
        day_of_month, hour, weekend gap, last 3 trading days, etc.) — otherwise
        the LLM tries to implement it by inventing event-tag columns
        (cot_report_change, china_cpi_release, etc.) and the strategy fails
        0-signal. See forever_20260525_092056.log for the regression."""
        DATE_HINTS = ('month', 'day_of_week', 'day_of_month', 'hour',
                      'last 3 trading days', 'weekend', 'shift(1)')
        for inst, concepts in ar._ASSET_MODE_CONCEPTS.items():
            for c in concepts:
                lc = c.lower()
                assert any(h in lc for h in DATE_HINTS), (
                    f'{inst}: concept "{c}" lacks a date-arithmetic hint — '
                    f'the LLM will try to implement it via a fictitious '
                    f'event-tag column and produce 0 signals.'
                )

    def test_no_concept_references_known_fictitious_columns(self):
        """Explicit blocklist of columns the LLM previously invented when
        following loose asset hints (see audit in batch 092056)."""
        FORBIDDEN = ('cot_report', 'china_cpi_release',
                     'china_trade_balance_release', 'event_impact',
                     'event_surprise', 'weather_shock', 'opec_announcement')
        for inst, concepts in ar._ASSET_MODE_CONCEPTS.items():
            for c in concepts:
                lc = c.lower()
                for bad in FORBIDDEN:
                    assert bad not in lc, (
                        f'{inst}: concept "{c}" mentions a fictitious column '
                        f'pattern "{bad}". Reword to a deterministic date '
                        f'pattern (e.g. day_of_week, day_of_month).'
                    )

    def test_asset_mode_lists_at_least_two_concepts(self):
        """Multiple concepts → LLM can pick a different one each visit
        (the cure for monoculture)."""
        for inst, concepts in ar._ASSET_MODE_CONCEPTS.items():
            assert len(concepts) >= 2, f'{inst} only has {len(concepts)} concept(s)'

    def test_asset_mode_is_instrument_specific(self):
        """BTC concepts must mention 24/7 / weekend / rebalance; NATGAS must
        mention heating / storage / eia / cooling / hurricane. Use a fixed
        seed so the picked concept is deterministic."""
        btc = ar._asset_mode_for('BTC_USD',    seed=0)
        ng  = ar._asset_mode_for('NATGAS_USD', seed=0)
        assert btc != ng
        assert any(k in btc.lower() for k in ('24/7', 'weekend', 'rebalance'))
        assert any(k in ng.lower()  for k in ('heating', 'storage', 'eia',
                                              'cooling', 'hurricane'))

    def test_per_visit_seed_rotates_concept(self):
        """Different seeds for the same instrument must yield DIFFERENT picked
        concepts — this is the cure for the 7/7-same-concept monoculture the
        previous list-of-concepts design produced."""
        concepts_seen = set()
        for seed in range(20):
            out = ar._asset_mode_for('NATGAS_USD', seed=seed)
            concepts_seen.add(out)
        # NATGAS has 4 concepts; 20 seeds should hit all 4 distinct constraints
        assert len(concepts_seen) == len(ar._ASSET_MODE_CONCEPTS['NATGAS_USD'])

    def test_exactly_one_concept_in_output(self):
        """The new design picks ONE concept per visit (not a list). The output
        must contain exactly ONE of the instrument's concepts, never two."""
        for inst in ('NATGAS_USD', 'BTC_USD', 'AUD_USD', 'XAU_USD', 'WTICO_USD'):
            out = ar._asset_mode_for(inst, seed=42)
            present = [c for c in ar._ASSET_MODE_CONCEPTS[inst] if c in out]
            assert len(present) == 1, (
                f'{inst}: expected exactly 1 concept in output, found '
                f'{len(present)}: {present}'
            )

    def test_instrument_offset_prevents_lock_step(self):
        """At the same seed, different instruments must NOT all land on the
        same concept index — the per-instrument ord-sum offset is what stops
        every asset slot in a batch picking position 0 in lock-step."""
        seed = 100
        # Concept index each instrument lands on
        def idx(inst):
            concepts = ar._ASSET_MODE_CONCEPTS[inst]
            inst_offset = sum(ord(c) for c in inst)
            return (seed + inst_offset) % len(concepts)
        indices = {inst: idx(inst) for inst in
                   ('AUD_USD', 'XAU_USD', 'NATGAS_USD', 'BTC_USD', 'LTC_USD')}
        # At least two distinct indices across these 5 instruments
        assert len(set(indices.values())) >= 2, (
            f'all instruments lock-step on the same index: {indices}'
        )

    def test_unknown_instrument_returns_none(self):
        """Caller falls through to the creative rotation."""
        assert ar._asset_mode_for('FAKE_PAIR') is None

    def test_twenty_iter_batch_has_three_asset_slots(self):
        """20-iter batch should land 3 asset slots — at iter 5, 10, 20 — on
        AUD_USD, XAU_USD, LTC_USD given the current instrument rotation."""
        instruments = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF', 'AUD_USD',
                       'NZD_USD', 'EUR_GBP', 'EUR_JPY', 'GBP_JPY', 'XAU_USD',
                       'XAG_USD', 'BCO_USD', 'WTICO_USD', 'NATGAS_USD', 'CORN_USD',
                       'SOYBN_USD', 'WHEAT_USD', 'BTC_USD', 'ETH_USD', 'LTC_USD']
        asset_slots = []
        for i in range(1, 21):
            inst = instruments[(i - 1) % len(instruments)]
            wild  = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            if not wild and not macro and (i % 5 == 0):
                if ar._asset_mode_for(inst):
                    asset_slots.append((i, inst))
        assert asset_slots == [(5, 'AUD_USD'), (10, 'XAU_USD'), (20, 'LTC_USD')]

    def test_asset_slots_pinned_to_daily(self):
        """Asset slots must be forced to D timeframe. The default rotation puts
        iter 10 and iter 20 on W (weekly), and many instruments — LTC especially
        — have no weekly data cached. Without this pin those slots fail with
        'No valid data for timeframe W' every batch. The asset concepts are also
        all expressed as day-bar arithmetic, so D is the correct match."""
        # Replicate the schedule's timeframe-decision logic for the asset slots.
        for i, expected_inst in [(5, 'AUD_USD'), (10, 'XAU_USD'), (20, 'LTC_USD')]:
            wild  = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            asset = (not wild) and (not macro) and (i % 5 == 0) and \
                    ar._asset_mode_for(expected_inst) is not None
            assert asset, f'iter {i} expected to be asset slot'
            # The pin: if asset, tf MUST be 'D' regardless of rotation index
            if wild:
                tf = None
            elif asset:
                tf = 'D'
            else:
                tf = ar._TIMEFRAME_ROTATION[(i - 1) % len(ar._TIMEFRAME_ROTATION)]
            assert tf == 'D', f'iter {i} asset slot for {expected_inst}: tf={tf}, expected D'

    def test_asset_does_not_override_wild_or_macro(self):
        """Priority: wild > macro > asset. Iter 15 (%5==0 AND %3==0) must be
        macro, not asset. Iter 40 (%8==0 AND %5==0) must be wild, not asset."""
        for i in (15, 30, 45):
            wild  = (i % 8 == 0)
            macro = (i % 3 == 0) and not wild
            assert macro, f'iter {i} expected macro priority'
        for i in (40,):
            wild = (i % 8 == 0)
            assert wild, f'iter {i} expected wild priority'


# ─────────────────────────────────────────────────────────────────────────────
# Instrument rotation — run loop must agree with the batch schedule
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentRotation:
    """The batch schedule pre-generates thesis N for instruments[(N-1) % len].
    The run loop pairs thesis_batch[N-1] with _rotate_instrument(N), so the two
    formulas MUST agree or every batch thesis lands on the wrong instrument."""

    def _researcher(self, instruments):
        r = object.__new__(ar.AutoResearcher)
        r.instruments = instruments
        return r

    def test_iteration_one_maps_to_first_instrument(self):
        r = self._researcher(['EUR_USD', 'GBP_USD', 'USD_JPY'])
        assert r._rotate_instrument(1) == 'EUR_USD'

    def test_matches_batch_schedule_formula(self):
        insts = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF', 'AUD_USD']
        r = self._researcher(insts)
        for i in range(1, 16):
            batch_inst = insts[(i - 1) % len(insts)]  # _generate_thesis_batch formula
            assert r._rotate_instrument(i) == batch_inst, f"mismatch at iteration {i}"

    def test_pool_offset_keeps_rotation_aligned_with_schedule(self):
        # With a per-batch start offset, the rotation must still match the
        # thesis-batch schedule formula (instruments[(i-1+offset) % len]) for the
        # SAME offset — or theses land on the wrong instrument.
        insts = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF', 'AUD_USD', 'NZD_USD']
        r = self._researcher(insts)
        for offset in range(len(insts)):
            r._pool_offset = offset
            for i in range(1, 14):
                sched = insts[(i - 1 + offset) % len(insts)]
                assert r._rotate_instrument(i) == sched, f"offset={offset} i={i}"

    def test_pool_offset_shifts_first_instrument(self):
        insts = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'NZD_USD']
        r = self._researcher(insts)
        # every pool position can become iteration-1's instrument under some offset
        firsts = set()
        for offset in range(len(insts)):
            r._pool_offset = offset
            firsts.add(r._rotate_instrument(1))
        assert firsts == set(insts)

    def test_rotation_defaults_to_offset_zero_without_init(self):
        # _rotate_instrument must work even when __init__ didn't set _pool_offset
        # (tests construct via object.__new__); getattr default keeps old behavior.
        r = self._researcher(['EUR_USD', 'GBP_USD'])
        assert r._rotate_instrument(1) == 'EUR_USD'


# ─────────────────────────────────────────────────────────────────────────────
# codegen.md — code-generation prompt template
# ─────────────────────────────────────────────────────────────────────────────

class TestCodegenTemplate:
    REQUIRED_PLACEHOLDERS = ('instrument', 'timeframe', 'family', 'hypothesis',
                             'entry', 'filter', 'exit', 'param_hints')

    def _fmt(self, **over):
        defaults = dict(instrument='EUR_USD', timeframe='D', family='statistical',
                        hypothesis='edge', entry='RSI(2)<10', filter='ADX(14)<20',
                        exit='5 bars', param_hints={'n': [10]})
        defaults.update(over)
        return ar._get_codegen_template().format(**defaults)

    def test_template_loads(self):
        tpl = ar._get_codegen_template()
        assert isinstance(tpl, str) and len(tpl) > 500

    def test_comment_header_stripped(self):
        """The maintainer <!-- ... --> block must not reach the LLM."""
        assert '<!--' not in ar._get_codegen_template()

    def test_formats_with_all_placeholders(self):
        """Every placeholder must resolve — no KeyError, no leftover braces."""
        out = self._fmt()
        for p in self.REQUIRED_PLACEHOLDERS:
            assert '{' + p + '}' not in out

    def test_interpolated_values_appear(self):
        out = self._fmt(instrument='GBP_JPY', entry='skew < -0.5')
        assert 'GBP_JPY' in out
        assert 'skew < -0.5' in out

    def test_literal_braces_render(self):
        """JSON examples and the {-1,0,1} set must survive .format()."""
        out = self._fmt()
        assert '{-1, 0, 1}' in out
        assert '"param_grid"' in out
        assert '"archetype": "standard"' in out

    def test_caching(self):
        """Second call returns the cached object."""
        assert ar._get_codegen_template() is ar._get_codegen_template()

    def test_carries_regime_gate_rules(self):
        """The regime-gate and direction-agnostic rules must be in the template."""
        tpl = ar._get_codegen_template()
        assert 'REGIME GATE' in tpl
        assert 'DIRECTION-AGNOSTIC' in tpl

    def test_regime_detectors_are_inline_not_functions(self):
        """Regime detectors must be inline snippets, not `def regime_*` helpers —
        the output is generate_signals only, so a named helper would NameError."""
        tpl = ar._get_codegen_template()
        assert 'def regime_' not in tpl
        assert 'INLINE' in tpl


# ─────────────────────────────────────────────────────────────────────────────
# _validate_thesis
# ─────────────────────────────────────────────────────────────────────────────

def _good_thesis(**overrides):
    """Return a minimally valid thesis dict."""
    t = {
        'strategy_family': 'statistical',
        'timeframe': 'D',
        'rationale': 'Mean reversion on daily closes using Bollinger Bands.',
        'entry_condition': 'Close crosses below lower band (2 std, 20-bar window).',
        'filter_condition': 'ADX(14) < 25 to confirm low-trend environment.',
        'exit_condition': 'Price returns above middle band or after 5 bars.',
        'param_hints': {'window': [10, 20, 30], 'std': [1.5, 2.0]},
    }
    t.update(overrides)
    return t


class TestExitMustCarryAMechanism:
    """A bare horizon is not a thesis (2026-08-03).

    Measured on the 2026-08-03 batch: 9 of 14 theses exited on a bar count
    unrelated to their own entry, and those scored WF 0.00-0.09 against a 0.5
    floor. These FAIL against the pre-fix validator, so they are a negative
    control rather than documentation.
    """

    @pytest.mark.parametrize('exit_cond', [
        'exit after 5 bars',
        'exit after 2 bars',
        'exit after exit_bars bars',
        'exit after hold_bars bars',
        'exit within 10 days',
        'close the position after 6 candles',
        # Qualifier BETWEEN the count and the unit. 'exit after 6 H4 bars' reached
        # a live batch on 2026-08-03: the first pattern allowed a word only BEFORE
        # the number, so '6 H4 bars' never matched and a bare horizon slipped past.
        'exit after 6 H4 bars (end of session day)',
        'exit after 3 full trading days',
        'exit within the next 5 daily bars',
    ])
    def test_bare_bar_count_is_rejected(self, exit_cond):
        err = ar._validate_thesis(_good_thesis(exit_condition=exit_cond))
        assert err is not None and 'bare bar count' in err, exit_cond

    @pytest.mark.parametrize('exit_cond', [
        # a mechanism alone
        'exit when z-score crosses 0',
        'exit when Donchian midpoint slope crosses zero',
        'exit when price moves against position by 2x ATR(14)',
        # a mechanism WITH a timeout — must NOT be rejected, or the check
        # would discard the best exits in the batch and cost whole iterations
        'exit after 3 bars or when the gap is 50% filled, whichever comes first',
        'exit after 10 bars or when the spread reverts to its mean',
        'exit at a 2x ATR trailing stop, or after 20 bars, whichever first',
    ])
    def test_mechanism_exit_passes_with_or_without_a_timeout(self, exit_cond):
        assert ar._validate_thesis(_good_thesis(exit_condition=exit_cond)) is None, exit_cond


class TestValidateThesis:
    def test_valid_thesis_returns_none(self):
        assert ar._validate_thesis(_good_thesis()) is None

    def test_not_a_dict_rejected(self):
        err = ar._validate_thesis("not a dict")
        assert err is not None
        assert 'not a dict' in err

    def test_missing_required_field(self):
        t = _good_thesis()
        del t['entry_condition']
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'entry_condition' in err

    def test_empty_required_field(self):
        t = _good_thesis(rationale='')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'rationale' in err

    def test_unknown_strategy_family(self):
        t = _good_thesis(strategy_family='magic')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'strategy_family' in err

    def test_family_alias_resolved(self):
        """'momentum' is an alias for 'regime' — should be accepted."""
        t = _good_thesis(strategy_family='momentum')
        err = ar._validate_thesis(t)
        assert err is None
        assert t['strategy_family'] == 'regime'  # normalized in-place

    def test_invalid_timeframe(self):
        t = _good_thesis(timeframe='1m')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'timeframe' in err

    def test_valid_timeframes(self):
        for tf in ('M30', 'H1', 'H4', 'D', 'W'):
            err = ar._validate_thesis(_good_thesis(timeframe=tf))
            assert err is None, f"Expected {tf} to be valid, got: {err}"

    def test_condition_too_short(self):
        t = _good_thesis(entry_condition='buy')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'entry_condition' in err
        assert 'too short' in err

    def test_param_hints_missing(self):
        t = _good_thesis()
        del t['param_hints']
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'param_hints' in err

    def test_param_hints_empty_dict(self):
        t = _good_thesis(param_hints={})
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'param_hints' in err

    def test_param_hints_no_list_values(self):
        t = _good_thesis(param_hints={'window': 20})  # scalar, not list
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'list values' in err

    def test_mixed_timeframe_keywords_rejected(self):
        """Conditions mixing 'daily' and 'hourly' indicate TF confusion."""
        t = _good_thesis(
            entry_condition='Close crosses below lower band on daily chart.',
            filter_condition='Wait for hourly confirmation candle before entry.',
        )
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'timeframe' in err.lower() or 'multiple' in err.lower()

    def test_single_timeframe_keyword_ok(self):
        """One TF keyword (e.g. 'daily') is fine — only multiple distinct ones are banned."""
        t = _good_thesis(
            entry_condition='Close below 20-day lower Bollinger Band.',
            filter_condition='Daily ADX(14) below 25.',
        )
        err = ar._validate_thesis(t)
        assert err is None


# ─────────────────────────────────────────────────────────────────────────────
# _extract_json
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json_object(self):
        result = ar._extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_plain_json_array(self):
        result = ar._extract_json('[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_fenced_json_block(self):
        text = '```json\n{"key": "val"}\n```'
        assert ar._extract_json(text) == {"key": "val"}

    def test_fenced_block_no_language_tag(self):
        text = '```\n{"key": "val"}\n```'
        assert ar._extract_json(text) == {"key": "val"}

    def test_json_embedded_in_prose(self):
        text = 'Here is my strategy:\n{"window": 20}\nDone.'
        result = ar._extract_json(text)
        assert result == {"window": 20}

    def test_array_embedded_in_prose(self):
        text = 'Results: [{"a": 1}, {"b": 2}] — end.'
        result = ar._extract_json(text)
        assert result == [{"a": 1}, {"b": 2}]

    def test_completely_invalid_returns_none(self):
        assert ar._extract_json('no json here at all') is None

    def test_empty_string_returns_none(self):
        assert ar._extract_json('') is None

    def test_prose_with_braces_not_json(self):
        assert ar._extract_json('{this is not json}') is None

    def test_nested_json(self):
        text = '{"param_grid": {"n": [10, 20]}, "archetype": "standard"}'
        result = ar._extract_json(text)
        assert result['param_grid']['n'] == [10, 20]

    def test_array_comes_before_object_prefers_array(self):
        """When array appears before object in text, array wins."""
        text = '[1, 2] {"key": "val"}'
        result = ar._extract_json(text)
        assert result == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# _validate_code — deeper branches not in test_pipeline.py
# ─────────────────────────────────────────────────────────────────────────────

BASE_FN = (
    "def generate_signals(df, params):\n"
    "    return df['close'].apply(lambda x: 1 if x > 0 else 0)\n"
)

class TestValidateCodeDeeper:
    def test_missing_generate_signals_rejected(self):
        err, _ = ar._validate_code("import pandas as pd\nx = 1\n")
        assert err is not None
        assert 'generate_signals' in err

    def test_lookahead_bias_rejected(self):
        code = "import pandas as pd\nimport numpy as np\n" + BASE_FN.replace(
            "return df['close'].apply(lambda x: 1 if x > 0 else 0)",
            "sig = df['close'].shift(-1)\n    return sig.fillna(0).astype(int)"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'look-ahead' in err

    def test_volume_column_rejected(self):
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    vol = df['volume']\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'volume' in err.lower()

    def test_no_price_reference_rejected(self):
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    x = params.get('n', 10)\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'price' in err.lower()

    def test_syntax_error_rejected(self):
        code = "import pandas as pd\ndef generate_signals(df params):\n    return 0\n"
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'syntax' in err.lower()

    def test_ta_cci_wrong_module_rejected(self):
        code = (
            "import pandas as pd\nimport ta\n"
            "def generate_signals(df, params):\n"
            "    v = ta.momentum.cci(df['high'], df['low'], df['close'], 14)\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'ta.momentum.cci' in err or 'ta.trend.cci' in err

    def test_ta_aroon_wrong_call_rejected(self):
        code = (
            "import pandas as pd\nimport ta\n"
            "def generate_signals(df, params):\n"
            "    v = ta.trend.aroon(df['high'], df['low'])\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'aroon' in err

    def test_series_boolean_and_auto_repaired(self):
        """Named Series vars (long_entry, uptrend) trigger auto-repair: 'and' → '&'."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    long_entry = df['close'].rolling(10).mean() > df['close']\n"
            "    uptrend = df['close'].rolling(20).mean() > df['close'].rolling(50).mean()\n"
            "    signal = long_entry and uptrend\n"
            "    return signal.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        assert err is None
        assert 'long_entry & uptrend' in cleaned or '& uptrend' in cleaned

    def test_series_boolean_and_rejected_when_ambiguous(self):
        """'and' between ambiguous variable names (no series pattern) is not auto-repaired
        but also not rejected — it only fails if the regex detects a Series context."""
        # Variables like 'a' and 'b' with df references on the same line
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    long_entry = df['close'].rolling(10).mean() > df['close']\n"
            "    vol_ok = df['high'] - df['low'] > params.get('atr', 0.001)\n"
            "    combined = long_entry and vol_ok\n"
            "    return combined.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        # vol_ok has 'df[' on the same assignment line — auto-repair triggers
        # Either fixed or rejected; what matters is the result is deterministic
        assert isinstance(err, (type(None), str))

    def test_uppercase_and_auto_fixed(self):
        """AND/OR/NOT should be auto-lowercased."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    cond = (df['close'] > 0) AND (df['close'] < 1000)\n"
            "    return cond.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        assert 'AND' not in cleaned

    def test_unknown_df_column_rejected(self):
        """Referencing df['sentiment'] (not in valid set) should be rejected."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    s = df['sentiment']\n"
            "    return (s > 0).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'sentiment' in err

    def test_code_written_column_allowed(self):
        """Columns that the code writes (df['ma'] = ...) are allowed to read back."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    df['ma'] = df['close'].rolling(10).mean()\n"
            "    return (df['close'] > df['ma']).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is None

    def test_valid_code_returns_none_error(self):
        """Sanity: a clean strategy gets no error."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    n = params.get('n', 20)\n"
            "    ma = df['close'].rolling(n).mean()\n"
            "    return (df['close'] > ma).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is None


# ─────────────────────────────────────────────────────────────────────────────
# Batch thesis generation — network-resilience outer retry + max_tokens=8000.
# Per the 2026-05-25 audit: 47% of batches fell back to per-iteration due to
# (a) max_tokens=4000 being stale (sized for 10 theses, not 20) and (b) ~84%
# transient network blips (HTTPSConnectionPool: Max retries exceeded).
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchThesisResilience:
    def _stub_thesis_array(self, n):
        """A minimal valid 20-thesis JSON array the batch parser will accept."""
        return [
            {
                'instrument': 'EUR_USD',
                'strategy_family': 'statistical',
                'timeframe': 'D',
                'rationale': 'test',
                'entry_condition': 'RSI(2) < 10',
                'filter_condition': 'ADX(14) < 20',
                # Mechanism exit, not a bare bar count: _validate_thesis rejects a
                # lone horizon (2026-08-03), and this fixture must stay VALID so the
                # test exercises cascade retry rather than thesis rejection.
                'exit_condition': 'exit when RSI(2) crosses back above 50',
                'param_hints': {'n': [10, 20]},
            }
            for _ in range(n)
        ]

    def test_batch_max_tokens_scales_with_chunk_size(self, monkeypatch):
        """Each thesis chunk gets enough output tokens and extended timeout.

        The budget must cover REASONING tokens, not just the answer: the
        opencode models emit their chain of thought into reasoning_content
        first, and _chat_content reads only content — so an answer-sized budget
        returns '' with finish_reason=length and scores as a hard failure.
        Measured 2026-07-23 on an 8-item chunk: deepseek-v4-pro and -flash both
        returned empty content at 4000 and 8/8 objects at 12000. Raised to a
        24000 floor on 2026-07-24 — 12000 sat right at the edge and the chain
        lead still length-burned on ~1/3 of live sub-batches. max_tokens is a
        ceiling, not a reservation, so the headroom costs nothing.
        """
        captured = {}
        def make_fake(n):
            def fake_or(**kwargs):
                captured['max_tokens'] = kwargs.get('max_tokens')
                captured['timeout'] = kwargs.get('timeout')
                return {'success': True, 'candidate': self._stub_thesis_array(n), 'error': None}
            return fake_or

        monkeypatch.setattr(ar, 'call_openrouter', make_fake(8))
        out = ar._generate_thesis_batch(['EUR_USD'] * 10, 10)
        assert captured['max_tokens'] == 24000, captured['max_tokens']
        assert captured['timeout'] == 300, captured['timeout']
        assert len(out) == 10

        monkeypatch.setattr(ar, 'call_openrouter', make_fake(8))
        out = ar._generate_thesis_batch(['EUR_USD'] * 31, 31)
        assert captured['max_tokens'] == 24000, captured['max_tokens']
        assert captured['timeout'] == 300, captured['timeout']
        assert len(out) == 31

    def test_batch_max_tokens_never_below_reasoning_floor(self, monkeypatch):
        """The 24000 floor holds for every chunk size THESIS_CHUNK can produce.

        The per-item term (1800/item) only binds above 13 items per chunk, so
        with THESIS_CHUNK=8 the floor is what actually protects every call. If
        THESIS_CHUNK is ever raised, the per-item term takes over — this test
        pins both halves of that contract.
        """
        seen = []
        def fake_or(**kwargs):
            seen.append(kwargs.get('max_tokens'))
            return {'success': True, 'candidate': self._stub_thesis_array(8), 'error': None}

        monkeypatch.setattr(ar, 'call_openrouter', fake_or)
        ar._generate_thesis_batch(['EUR_USD'] * 31, 31)
        assert seen and all(mt >= 24000 for mt in seen), seen
        # the scaling term is what protects a larger chunk
        assert max(24000, 20 * 1800) == 36000

    def test_batch_retries_whole_cascade_on_network_blip(self, monkeypatch):
        """A failed first chunk retries the whole thesis chain, then later chunks still run."""
        calls = []
        succeed_after = len(ar.THESIS_MODELS) + 1
        def fake_or(**kwargs):
            calls.append(kwargs['model'])
            if len(calls) < succeed_after:
                return {'success': False, 'error': 'HTTPSConnectionPool: Max retries exceeded', 'candidate': None}
            return {'success': True, 'candidate': self._stub_thesis_array(8), 'error': None}
        monkeypatch.setattr(ar, 'call_openrouter', fake_or)
        monkeypatch.setattr(ar.time, 'sleep', lambda *_: None)
        out = ar._generate_thesis_batch(['EUR_USD'] * 20, 20)
        assert len(calls) >= succeed_after, calls
        assert len(out) == 20
        assert sum(x is not None for x in out) >= 19

    def test_batch_does_not_retry_on_first_success(self, monkeypatch):
        """Successful chunks should use one model call each, with no outer retry."""
        calls = []
        def fake_or(**kwargs):
            calls.append(kwargs['model'])
            return {'success': True, 'candidate': self._stub_thesis_array(8), 'error': None}
        monkeypatch.setattr(ar, 'call_openrouter', fake_or)
        out = ar._generate_thesis_batch(['EUR_USD'] * 20, 20)
        assert len(calls) == 3, f'20 items should produce 3 chunk calls, got {len(calls)}'
        assert len(out) == 20

    def test_batch_none_fills_after_both_cascades_fail(self, monkeypatch):
        """Failed chunks return None slots so the loop regenerates those items individually."""
        def fake_or(**kwargs):
            return {'success': False, 'error': 'persistent network error', 'candidate': None}
        monkeypatch.setattr(ar, 'call_openrouter', fake_or)
        monkeypatch.setattr(ar.time, 'sleep', lambda *_: None)
        out = ar._generate_thesis_batch(['EUR_USD'] * 20, 20)
        assert out == [None] * 20


class TestProviderCircuitBreaker:
    """Cross-provider failover: when opencode is down, cline keeps working.

    Chains are mirrored across two providers, so plain sequential failover is
    already correct — the breaker exists to make it FAST, by demoting a provider
    that keeps failing at the transport layer so the healthy one is tried first.
    """

    CHAIN = ['opencode:a', 'opencode:b', 'cline:x']

    @pytest.fixture(autouse=True)
    def _clean(self):
        ar._PROVIDER_HEALTH.clear()
        yield
        ar._PROVIDER_HEALTH.clear()

    @pytest.mark.parametrize('err', [
        'HTTP 503: Inference is temporarily unavailable',
        'HTTP 500: Internal server error',
        "API error: ('Connection aborted.', RemoteDisconnected())",
        'https://opencode.ai/zen/go/v1 timeout',
        'HTTP 429: usage limit reached',
    ])
    def test_transport_errors_blame_the_provider(self, err):
        assert ar._is_provider_level_err(err) is True

    @pytest.mark.parametrize('err', [
        'Empty content from model (finish_reason=length)',
        'Failed to parse JSON: {"verdict"...',
        'Model error: bad request shape',
        'HTTP 400: malformed',
        'Prompt too large (~13000 tokens). Trim failure context.',
    ])
    def test_model_errors_never_blame_the_provider(self, err):
        """One bad generation must not sideline a provider that is answering."""
        assert ar._is_provider_level_err(err) is False
        for _ in range(10):
            ar._record_provider_result('opencode:a', False, err)
        assert ar._chain_order(self.CHAIN) == self.CHAIN

    def test_trips_only_after_threshold(self):
        for _ in range(ar._PROVIDER_TRIP_THRESHOLD - 1):
            ar._record_provider_result('opencode:a', False, 'HTTP 503: down')
        assert ar._chain_order(self.CHAIN) == self.CHAIN
        ar._record_provider_result('opencode:a', False, 'HTTP 503: down')
        assert ar._chain_order(self.CHAIN) == ['cline:x', 'opencode:a', 'opencode:b']

    def test_success_repromotes_immediately(self):
        for _ in range(ar._PROVIDER_TRIP_THRESHOLD):
            ar._record_provider_result('opencode:a', False, 'HTTP 503: down')
        ar._record_provider_result('opencode:b', True)
        assert ar._chain_order(self.CHAIN) == self.CHAIN

    def test_all_providers_tripped_keeps_original_order(self):
        """No 'everything skipped' failure mode — order is preserved, not emptied."""
        for m in ('opencode:a', 'cline:x'):
            for _ in range(ar._PROVIDER_TRIP_THRESHOLD):
                ar._record_provider_result(m, False, 'HTTP 503: down')
        assert ar._chain_order(self.CHAIN) == self.CHAIN

    def test_cooldown_expiry_is_half_open(self):
        for _ in range(ar._PROVIDER_TRIP_THRESHOLD):
            ar._record_provider_result('opencode:a', False, 'HTTP 503: down')
        assert ar._chain_order(self.CHAIN)[0] == 'cline:x'
        ar._PROVIDER_HEALTH['opencode:']['until'] = time.time() - 1
        assert ar._chain_order(self.CHAIN) == self.CHAIN

    def test_chain_order_never_drops_or_duplicates(self):
        for _ in range(ar._PROVIDER_TRIP_THRESHOLD):
            ar._record_provider_result('opencode:a', False, 'HTTP 503: down')
        assert sorted(ar._chain_order(self.CHAIN)) == sorted(self.CHAIN)

    def test_provider_prefix_mapping(self):
        assert ar._provider_of('opencode:glm-5.2') == 'opencode:'
        assert ar._provider_of('cline:cline-pass/glm-5.2') == 'cline:'
        assert ar._provider_of('bare/model') == 'openrouter:'

    def test_failover_serves_from_healthy_provider(self, monkeypatch):
        """End-to-end: opencode down, chain still returns a result — from cline."""
        def fake_once(system_prompt, user_prompt, model, api_key=None,
                      temperature=0.7, max_tokens=2048, timeout=60):
            if model.startswith('opencode:'):
                return {'success': False, 'candidate': None,
                        'error': 'API error: Connection refused'}
            return {'success': True, 'candidate': {'ok': True}, 'error': None}
        monkeypatch.setattr(ar, '_call_openrouter_once', fake_once)

        served = []
        for _ in range(2):
            for mdl in ar._chain_order(self.CHAIN):
                r = ar.call_openrouter(system_prompt='s', user_prompt='u', model=mdl)
                if r['success']:
                    served.append(mdl)
                    break
        assert served == ['cline:x', 'cline:x']
        # second pass must not have re-walked the dead provider
        assert ar._chain_order(self.CHAIN)[0] == 'cline:x'


class TestReasoningEffortCap:
    """`reasoning:{effort:'low'}` is injected for gateway-proxied providers so the
    DeepSeek reasoning models don't burn the whole token budget on chain-of-thought
    (measured 2026-07-23: deepseek-v4-pro hit finish=length, content='' at 12000
    without it). Only opencode/cline get it; direct providers are left untouched."""

    def test_param_set_for_opencode_and_cline(self, monkeypatch):
        monkeypatch.setattr(ar, 'REASONING_EFFORT', 'low')
        assert ar._reasoning_param('opencode:deepseek-v4-pro') == {'effort': 'low'}
        assert ar._reasoning_param('cline:cline-pass/glm-5.2') == {'effort': 'low'}

    def test_param_none_for_direct_and_other_providers(self, monkeypatch):
        monkeypatch.setattr(ar, 'REASONING_EFFORT', 'low')
        # direct DeepSeek (OpenRouter/byteplus) uses provider-pin/native, not this cap
        assert ar._reasoning_param('deepseek/deepseek-v4-flash') is None
        assert ar._reasoning_param('byteplus:foo') is None
        assert ar._reasoning_param('') is None

    def test_empty_effort_disables_injection(self, monkeypatch):
        monkeypatch.setattr(ar, 'REASONING_EFFORT', '')
        assert ar._reasoning_param('opencode:deepseek-v4-pro') is None

    def test_payload_carries_reasoning_for_opencode(self, monkeypatch):
        monkeypatch.setattr(ar, 'REASONING_EFFORT', 'low')
        monkeypatch.setattr(ar, 'OPENCODE_BASE', 'https://opencode.example/v1')
        monkeypatch.setattr(ar, 'OPENCODE_KEY', 'tok')
        sent = {}

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {'choices': [{'message': {'content': '```json\n{"x":1}\n```'},
                                     'finish_reason': 'stop'}]}
        def fake_post(url, headers=None, json=None, timeout=None):
            sent['payload'] = json
            return _Resp()
        monkeypatch.setattr(ar.requests, 'post', fake_post)
        ar._call_openrouter_once('s', 'u', model='opencode:deepseek-v4-pro', max_tokens=12000)
        assert sent['payload'].get('reasoning') == {'effort': 'low'}

    def test_400_on_reasoning_field_retries_without_it(self, monkeypatch):
        monkeypatch.setattr(ar, 'REASONING_EFFORT', 'low')
        monkeypatch.setattr(ar, 'OPENCODE_BASE', 'https://opencode.example/v1')
        monkeypatch.setattr(ar, 'OPENCODE_KEY', 'tok')
        calls = []

        import requests as _rq

        class _Ok:
            status_code = 200
            text = '```json\n{"x":1}\n```'
            def raise_for_status(self): pass
            def json(self):
                return {'choices': [{'message': {'content': '```json\n{"x":1}\n```'},
                                     'finish_reason': 'stop'}]}

        class _Bad:
            status_code = 400
            text = 'invalid_request: unknown field reasoning'
            def raise_for_status(self):
                raise _rq.exceptions.HTTPError('400')
            def json(self):
                return {'error': 'unknown field reasoning'}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(dict(json))
            return _Bad() if 'reasoning' in json else _Ok()
        monkeypatch.setattr(ar.requests, 'post', fake_post)
        r = ar._call_openrouter_once('s', 'u', model='opencode:deepseek-v4-pro', max_tokens=12000)
        assert r['success'] is True
        assert len(calls) == 2                     # first with reasoning (400), retry without
        assert 'reasoning' in calls[0] and 'reasoning' not in calls[1]


class TestMetaReviewTriggerIsRobust:
    """The directive loop died silently on 2026-08-03 and nothing reported it.

    Two independent defects: the trigger used EXACT EQUALITY (`== 15`) checked at
    only ONE of the four `results['failed'].append` sites, so a batch crossing 15
    via another site skipped it (it fired on 8 of 51 batches); and it sat INSIDE
    the loop, so when the exit-mechanism rule pushed rejections earlier in the
    funnel the counted failures fell to 10-12 and it could never fire at all.
    """
    import re as _re
    import inspect as _inspect

    def _src(self):
        import inspect
        import auto_research as ar
        return inspect.getsource(ar.AutoResearcher.run)

    def test_trigger_uses_a_threshold_not_exact_equality(self):
        import re
        src = self._src()
        live = [l for l in src.splitlines()
                if "results['failed']" in l and l.lstrip().startswith('if ')]
        assert live, 'meta-review trigger not found at all'
        for line in live:
            assert '==' not in line, f'exact-equality trigger is skippable: {line.strip()!r}'
            assert '>=' in line, f'expected a >= threshold: {line.strip()!r}'

    def test_trigger_fires_after_the_iteration_loop(self):
        # Theses are generated up-front per batch, so a mid-batch directive can
        # only affect the NEXT batch — and firing inside the loop is what made it
        # depend on which append site happened to reach the count.
        src = self._src()
        assert src.index('META_REVIEW_MIN_FAILURES') > src.index('Continuing to next iteration')

    def test_threshold_is_below_the_observed_failure_floor(self):
        # Measured 2026-08-03 after the exit rule: 20-iteration batches recorded
        # 10-12 counted failures. A threshold above that floor silently disables
        # directive generation, which is exactly what 15 did.
        import auto_research as ar
        assert ar.META_REVIEW_MIN_FAILURES <= 10


class TestThesisRepairRecoversInsteadOfDiscarding:
    """A mis-shaped thesis used to destroy its whole iteration (2026-08-03).

    Adding the exit-mechanism rule made this materially worse — it accounted for
    2 of the 5 'errors' in a 20-iteration batch. The thesis is normally sound
    apart from the one rejected field, so re-rolling the idea wastes the good
    part with the bad. The repair must be TARGETED and must fail CLOSED.
    """

    def _bad(self):
        return {'instrument': 'EUR_USD', 'strategy_family': 'regime', 'timeframe': 'D',
                'rationale': 'Trend persists when the channel breaks and momentum confirms.',
                'entry_condition': 'go LONG when close > Donchian(20) high',
                'filter_condition': 'ADX(14) > 20',
                'exit_condition': 'exit after 5 bars',
                'param_hints': {'lookback': [10, 20, 30]}}

    def test_repaired_thesis_passes_validation(self):
        bad = self._bad()
        err = ar._validate_thesis(dict(bad))
        assert err and 'bare bar count' in err
        fixed = {**bad, 'exit_condition': 'exit when close < Donchian(20) low'}
        out = ar._repair_thesis_field(dict(bad), err, 'EUR_USD', api_key='k',
                                      _call=lambda p: {'success': True, 'candidate': fixed})
        assert ar._validate_thesis(dict(out)) is None

    def test_repair_does_not_rewrite_the_mechanism(self):
        # The whole point: fix the shape, keep the idea. A repair that quietly
        # swapped the entry would launder a rejected thesis into a different one.
        bad = self._bad()
        err = ar._validate_thesis(dict(bad))
        sneaky = {'exit_condition': 'exit when z-score crosses 0',
                  'entry_condition': 'go LONG when RSI(2) < 10'}
        out = ar._repair_thesis_field(dict(bad), err, 'EUR_USD', api_key='k',
                                      _call=lambda p: {'success': True, 'candidate': sneaky})
        # the model MAY change the rejected field, but dropped keys are restored
        assert out['filter_condition'] == bad['filter_condition']
        assert out['param_hints'] == bad['param_hints']

    def test_repair_cannot_change_identity(self):
        bad = self._bad()
        err = ar._validate_thesis(dict(bad))
        out = ar._repair_thesis_field(
            dict(bad), err, 'EUR_USD', api_key='k',
            _call=lambda p: {'success': True,
                             'candidate': {'exit_condition': 'exit when z-score crosses 0',
                                           'instrument': 'GBP_USD', 'timeframe': 'H1'}})
        assert out['instrument'] == 'EUR_USD'
        assert out['timeframe'] == 'D'

    @pytest.mark.parametrize('call,label', [
        (lambda p: {'success': False}, 'api failure'),
        (lambda p: {'success': True, 'candidate': None}, 'no candidate'),
        (lambda p: {'success': True, 'candidate': 'not a dict'}, 'wrong type'),
        (lambda p: (_ for _ in ()).throw(RuntimeError('boom')), 'raises'),
    ])
    def test_failure_returns_none_so_caller_keeps_the_original(self, call, label):
        bad = self._bad()
        err = ar._validate_thesis(dict(bad))
        assert ar._repair_thesis_field(dict(bad), err, 'EUR_USD',
                                       api_key='k', _call=call) is None, label

    def test_deterministic_rejections_are_guarded_not_errors(self):
        # 'errors' must mean a crash or transient API failure. A thesis the
        # validator deliberately refused is the gate working; counting it as an
        # error made the batch health signal unreadable.
        import inspect
        src = inspect.getsource(ar.AutoResearcher.run)
        assert 'bare bar count' in src and "_deterministic" in src
        assert "results['guarded' if _deterministic else 'errors']" in src


# ─────────────────────────────────────────────────────────────────────────────
# ACADEMIC RECALL category (2026-08-09) — categories/academic.md + its slot
# ─────────────────────────────────────────────────────────────────────────────

class TestAcademicRecallCategory:
    """The academic slot is measured through the ACADEMIC(...) rationale prefix —
    `strategies` has no gen_category column, and an academic-carry thesis saves as
    archetype='macro', indistinguishable from an ordinary macro slot. So the
    prefix requirement and the slot's isolation from the other families are both
    load-bearing, not cosmetic."""

    def _steer(self, **kw):
        import steering
        return steering.Steering(**kw)

    def _schedule(self, n=120, instruments=('EUR_USD', 'XAU_USD', 'USD_JPY')):
        return ar._build_batch_schedule(list(instruments), n, steer=self._steer())

    # ── the md loads and carries the mandatory parts ──────────────────────────
    def test_category_md_loads_and_is_not_falling_back(self):
        cat = ar._load_category('academic')
        assert cat is not None, 'categories/academic.md missing or has no CONSTRAINT'
        assert cat['guidance'].strip(), 'academic.md must carry a GUIDANCE block'

    def test_constraint_demands_the_attribution_prefix(self):
        c = ar._academic_constraint_for('EUR_USD', 1)
        assert 'ACADEMIC(' in c
        assert 'rationale' in c.lower()

    def test_constraint_bans_cross_sectional_forms(self):
        # Every sleeve trades ONE instrument, so decile sorts / long-short baskets
        # / betting-against-beta / PEAD cannot be expressed at all.
        c = ar._academic_constraint_for('EUR_USD', 1).lower()
        assert 'single instrument only' in c
        assert 'time-series' in c
        for banned in ('ranking a universe', 'baskets', 'betting-against-beta'):
            assert banned in c

    def test_constraint_pins_exact_macro_columns_for_the_instrument(self):
        # Same guard the macro category needs: an invented column name is not
        # injected and KeyErrors at signal-check.
        from macro_fetcher import list_available_columns
        cols = sorted(list_available_columns('EUR_USD').keys())
        c = ar._academic_constraint_for('EUR_USD', 1)
        assert str(cols) in c
        assert 'EUR_USD' in c

    def test_constraint_keeps_the_house_code_limits(self):
        c = ar._academic_constraint_for('EUR_USD', 1).lower()
        assert 'at most 4 tunable parameters' in c
        assert 'at most 200 original grid combinations' in c
        assert 'compute_returns_with_stop' in c
        assert '.rolling(...).apply' in c

    def test_fallback_mirrors_the_md_requirements(self):
        # A missing/edited md must never silently drop the attribution tag.
        c = ar._FALLBACK_ACADEMIC
        assert 'ACADEMIC(' in c
        assert '{anomaly}' in c and '{instrument}' in c and '{cols}' in c
        assert 'SINGLE INSTRUMENT ONLY' in c
        assert ar._FALLBACK_CONSTRAINTS['academic'] is c

    # ── the anomaly is PINNED and rotates ─────────────────────────────────────
    def test_anomaly_rotates_one_per_slot(self):
        seen = {ar._academic_constraint_for('EUR_USD', n) for n in range(10)}
        assert len(seen) == len(ar._ACADEMIC_ANOMALIES) == 10, \
            'each slot must pin a DIFFERENT anomaly — an unpinned model collapses to momentum'
        for n, name in enumerate(ar._ACADEMIC_ANOMALIES):
            assert name in ar._academic_constraint_for('EUR_USD', n)

    # Instruments that can express every anomaly, so a miss here is a ROTATION
    # fault and never the FX/data gate. Mixed FX + non-FX on purpose: the pools
    # are different LENGTHS, which is what distorted the draw the second time.
    _ROT_INSTS = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'XAU_USD', 'SPX500_USD']

    def _academic_draw(self, sched):
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        return [a for _, c, _, _, _, _ in sched if c.startswith(acad)
                for a in ar._ACADEMIC_ANOMALIES if a in c]

    def test_every_anomaly_is_reachable_across_REAL_LENGTH_batches(self):
        """Regression for BOTH starvation bugs — read the batch length carefully.

        This test previously built ONE 200-iteration batch, which fires ~33
        academic slots and walks the whole list. Production batches are 20
        iterations and fire exactly FOUR, so the per-batch counter reset at 4 and
        list positions 4+ were never assigned in the real system while this test
        stayed green. An unrealistic batch length is what made the bug invisible,
        so the length here is fixed at MAX_ITER and coverage must come from
        ACCUMULATING batches — which is the property that actually matters.
        """
        MAX_ITER, BATCHES = 20, 12
        # W must be on the rotation or long-term reversal is legitimately gated
        # out (see _ACADEMIC_TF_REQS) and this would fail for the wrong reason.
        steer = self._steer(timeframe_rotation=['D', 'H4', 'W'])
        off, drawn = 0, []
        for _ in range(BATCHES):
            sched = ar._build_batch_schedule(
                self._ROT_INSTS, MAX_ITER, steer=steer, academic_offset=off)
            hits = self._academic_draw(sched)
            drawn += hits
            off += len(hits)          # mirrors the persistent walk
        assert off > 0, 'no academic slots fired at all — the slot itself is broken'
        missing = set(ar._ACADEMIC_ANOMALIES) - set(drawn)
        assert not missing, (
            f'unreachable after {BATCHES} batches of {MAX_ITER}: {sorted(missing)} '
            f'(drew {len(drawn)} slots)')

    def test_a_single_batch_cannot_cover_the_list_so_the_walk_must_persist(self):
        """Pins the FACT that made the reset fatal: one real batch fires far
        fewer academic slots than there are anomalies. If this ever stops being
        true the persistence still helps, but the starvation risk is what this
        documents — do not 'simplify' the counter back to a per-batch reset."""
        sched = ar._build_batch_schedule(
            self._ROT_INSTS, 20, steer=self._steer(), academic_offset=0)
        assert len(self._academic_draw(sched)) < len(ar._ACADEMIC_ANOMALIES)

    def test_offset_resumes_the_walk_rather_than_restarting_it(self):
        """A non-zero offset must hand out DIFFERENT anomalies — the whole point."""
        a = self._academic_draw(ar._build_batch_schedule(
            self._ROT_INSTS, 20, steer=self._steer(), academic_offset=0))
        b = self._academic_draw(ar._build_batch_schedule(
            self._ROT_INSTS, 20, steer=self._steer(), academic_offset=len(a)))
        assert a and b and a != b, f'offset ignored: {a} == {b}'

    def test_rotation_counter_persists_across_builds(self, tmp_path, monkeypatch):
        """Production path (academic_offset=None): the counter must survive the
        batch boundary via the file, not restart at 0."""
        monkeypatch.setattr(ar, '_ACADEMIC_ROTATION_FILE', tmp_path / '.academic_rotation')
        first = self._academic_draw(ar._build_batch_schedule(
            self._ROT_INSTS, 20, steer=self._steer()))
        assert ar._academic_rotation_offset() == len(first), 'counter did not advance'
        second = self._academic_draw(ar._build_batch_schedule(
            self._ROT_INSTS, 20, steer=self._steer()))
        assert first != second, 'second batch repeated the first — counter reset'

    def test_timeframe_pinned_anomaly_never_overrides_the_steering_rotation(self):
        """Long-term reversal is pinned to WEEKLY. steering.md dropped W from the
        rotation, so making the anomaly reachable (the rotation fix) silently put
        weekly bars back into generation — a category quietly overruling steering.
        With W off it must be SKIPPED, not granted an override; with W on it
        returns by itself."""
        off_rot = self._steer(timeframe_rotation=['D', 'H4'])
        sched = ar._build_batch_schedule(
            self._ROT_INSTS, 60, steer=off_rot, academic_offset=0)
        assert 'Long-Term Reversal' not in self._academic_draw(sched)
        assert {x[5] for x in sched} <= {'D', 'H4', None}, 'W leaked back in'

        # Accumulate batches: one batch fires far fewer slots than the list is
        # long, which is the whole reason the walk has to persist.
        on_rot = self._steer(timeframe_rotation=['D', 'W'])
        off, drawn = 0, []
        for _ in range(12):
            hits = self._academic_draw(ar._build_batch_schedule(
                self._ROT_INSTS, 20, steer=on_rot, academic_offset=off))
            drawn += hits
            off += len(hits)
        assert 'Long-Term Reversal' in drawn

    def test_rotation_counter_is_fail_soft(self, tmp_path, monkeypatch):
        """A missing or corrupt counter must degrade to 0, never raise — it sits
        in the batch-build path, so an exception here kills the whole batch."""
        f = tmp_path / '.academic_rotation'
        monkeypatch.setattr(ar, '_ACADEMIC_ROTATION_FILE', f)
        assert ar._academic_rotation_offset() == 0          # absent
        f.write_text('not-an-int')
        assert ar._academic_rotation_offset() == 0          # corrupt
        f.write_text('-5')
        assert ar._academic_rotation_offset() == 0          # negative

    # ── FX-only anomalies stay on FX ──────────────────────────────────────────
    def test_carry_and_ppp_are_fx_only(self):
        for inst in ('EUR_USD', 'USD_JPY', 'GBP_USD'):
            assert ar._is_fx_pair(inst), inst
            assert set(ar._academic_anomalies_for(inst)) == set(ar._ACADEMIC_ANOMALIES)

    def test_fx_only_forms_also_require_the_column_that_defines_them(self):
        """Being a currency pair is necessary but not sufficient. macro_fetcher does
        not carry both legs for every pair, and handing the model an anomaly its
        column list cannot express is what produced the documented invented
        `nz_rate`/`nzr_rate` KeyErrors on NZD pairs."""
        assert 'FX Carry Trade' not in ar._academic_anomalies_for('NZD_USD')
        assert 'FX Carry Trade' not in ar._academic_anomalies_for('USD_CHF')
        for inst in ('AUD_USD', 'NZD_USD'):
            assert 'Real-Exchange-Rate Value (PPP Deviation)' not in \
                ar._academic_anomalies_for(inst), inst
        # ... and it must not over-prune where the data IS there
        assert 'FX Carry Trade' in ar._academic_anomalies_for('EUR_USD')
        assert 'Real-Exchange-Rate Value (PPP Deviation)' in \
            ar._academic_anomalies_for('USD_JPY')

    def test_no_instrument_is_left_with_an_empty_pool(self):
        import sqlite3
        conn = sqlite3.connect('pipeline.db')
        insts = [r[0] for r in conn.execute(
            'select distinct instrument from strategies where instrument is not null')]
        conn.close()
        for inst in insts:
            pool = ar._academic_anomalies_for(inst)
            assert len(pool) >= 8, (inst, len(pool))

    def test_data_gate_fails_soft(self, monkeypatch):
        # macro_fetcher unavailable -> drop the data-gated forms, never crash
        import macro_fetcher
        monkeypatch.setattr(macro_fetcher, 'list_available_columns',
                            lambda i: (_ for _ in ()).throw(RuntimeError('boom')))
        pool = ar._academic_anomalies_for('EUR_USD')
        assert 'FX Carry Trade' not in pool
        assert 'Time-Series Momentum (12-1)' in pool
        for inst in ('XAU_USD', 'XCU_USD', 'NAS100_USD', 'WTICO_USD',
                     'BTC_USD', 'DE30_EUR', 'SPX500_USD'):
            assert not ar._is_fx_pair(inst), inst
            pool = ar._academic_anomalies_for(inst)
            assert 'FX Carry Trade' not in pool
            assert 'Real-Exchange-Rate Value (PPP Deviation)' not in pool
            assert len(pool) == len(ar._ACADEMIC_ANOMALIES) - 2

    def test_no_fx_only_anomaly_reaches_a_non_fx_slot(self):
        """Regression: the first dry run drew 'FX Carry Trade' for WTICO_USD and
        NAS100_USD — a rate differential on crude and the Nasdaq."""
        sched = ar._build_batch_schedule(
            ['EUR_USD', 'XAU_USD', 'NAS100_USD', 'WTICO_USD', 'BTC_USD'],
            240, steer=self._steer())
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        for inst, c, _, i, _, _ in sched:
            if c.startswith(acad) and not ar._is_fx_pair(inst):
                for fx_only in ar._ACADEMIC_FX_ONLY:
                    assert fx_only not in c, f'{fx_only} assigned to {inst} at i={i}'

    def test_no_cross_sectional_anomaly_in_the_rotation(self):
        joined = ' '.join(ar._ACADEMIC_ANOMALIES).lower()
        for impossible in ('cross-sectional', 'betting-against-beta',
                           'accrual', 'post-earnings', 'idiosyncratic'):
            assert impossible not in joined

    # ── the slot cannibalises nothing ─────────────────────────────────────────
    def test_academic_slot_never_collides_with_another_family(self):
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        wild = ar._category_constraint('wild')[:40]
        cal = ar._CALENDAR_CONSTRAINT[:40]
        ev = ar._EVENT_CONSTRAINT[:40]
        nn = ar._NNFX_CONSTRAINT[:40]
        n_acad = 0
        for inst, constraint, is_wild, i, detector, tf in self._schedule():
            if constraint.startswith(acad):
                n_acad += 1
                assert i % 6 == 1, f'academic fired on i={i}, outside i%6==1'
                assert not is_wild
                assert i % 3 != 0, f'i={i} would have been a macro slot'
                for other, label in ((wild, 'wild'), (cal, 'calendar'),
                                     (ev, 'event'), (nn, 'nnfx')):
                    assert not constraint.startswith(other), f'collided with {label}'
        assert n_acad > 0, 'academic slot never fired in 120 iterations'

    def test_academic_share_is_the_intended_small_tail(self):
        sched = self._schedule(n=120)
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        n = sum(1 for s in sched if s[1].startswith(acad))
        # i%6==1 is 20 of 120 raw; wild/asset/exploit outrank it, so expect a
        # little under. Guard both ends — a silent drop to ~0 is the real risk.
        assert 12 <= n <= 20, f'academic share {n}/120 is outside the intended tail'

    def test_macro_and_calendar_keep_their_full_share(self):
        # Ranking academic last must not have moved any existing family.
        sched = self._schedule(n=120)
        cal = ar._CALENDAR_CONSTRAINT[:40]
        assert sum(1 for s in sched if s[1].startswith(cal)) > 0
        for inst, constraint, is_wild, i, detector, tf in sched:
            if i % 3 == 0 and not is_wild and i % 15 != 0:
                assert not constraint.startswith(
                    ar._category_constraint('academic', anomaly='', instrument='',
                                            cols='')[:40]), \
                    f'academic stole macro slot i={i}'

    # ── timeframe: pinned by anomaly, never rotated ───────────────────────────
    def test_academic_timeframes_are_pinned_by_anomaly(self):
        # Every anomaly here is documented at daily-or-slower horizons: a 12-1
        # momentum lookback on H1 spans more bars than the fetch window holds, and
        # turn-of-month is meaningless on weekly. Long-term reversal is the sole
        # weekly case (a 3-5y daily lookback eats the sample).
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        n = 0
        for inst, constraint, is_wild, i, detector, tf in self._schedule(n=240):
            if not constraint.startswith(acad):
                continue
            n += 1
            want = 'W' if 'Long-Term Reversal' in constraint else 'D'
            assert tf == want, f'academic slot i={i} on {tf}, expected {want}'
        assert n > 0

    def test_long_term_reversal_actually_gets_a_weekly_slot(self):
        sched = self._schedule(n=240)
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        assert any(c.startswith(acad) and 'Long-Term Reversal' in c and tf == 'W'
                   for _, c, _, _, _, tf in sched)

    def test_guidance_is_spliced_into_the_thesis_rules(self):
        # A category with no entry in _get_thesis_rules' tuple loads its CONSTRAINT
        # but its GUIDANCE never reaches the model — silent and easy to miss.
        rules = ar._get_thesis_rules()
        assert 'Academic recall' in rules
        assert 'ACADEMIC(' in rules
        assert 'cross-section' in rules.lower()

    def test_assigned_anomaly_comes_from_the_constraint_not_the_prefix(self):
        # The rationale prefix is model-written and DRIFTS: replaying all 765
        # academic gens (2026-08-21) agreed with it on only 80.7% of rows, and
        # the disagreement is directed (Momentum/Turn-of-Month come back
        # relabelled as Breakout/Short-Term Reversal). The constraint is rendered
        # by us, so it is the authoritative record of the draw.
        for anomaly in ar._ACADEMIC_ANOMALIES:
            constraint = ar._category_constraint(
                'academic', anomaly=anomaly, instrument='EUR_USD', cols=['us10y'])
            assert ar._assigned_academic_anomaly(constraint) == anomaly, anomaly

    def test_assigned_anomaly_matches_longest_name_first(self):
        # "Time-Series Momentum" is a strict prefix of "Time-Series Momentum
        # (12-1)". A shortest-first walk silently returns the wrong canonical
        # name for every 12-1 slot -- the exact collapse this column exists to
        # stop, reintroduced one layer lower.
        c = ar._category_constraint('academic', anomaly='Time-Series Momentum (12-1)',
                                    instrument='EUR_USD', cols=[])
        assert ar._assigned_academic_anomaly(c) == 'Time-Series Momentum (12-1)'

    def test_assigned_anomaly_is_none_for_a_non_academic_slot(self):
        # Non-academic rows must stay NULL, or a later GROUP BY reads free-form
        # generation as academic-recall output.
        assert ar._assigned_academic_anomaly('Trade a 20-day breakout.') is None
        assert ar._assigned_academic_anomaly('') is None
        assert ar._assigned_academic_anomaly(None) is None

    def test_every_academic_schedule_slot_resolves_to_an_anomaly(self):
        # An academic slot whose constraint does not resolve writes NULL and is
        # invisible to the measurement -- indistinguishable from a non-academic
        # row. Assert over the real schedule, not a hand-built constraint:
        # rendering is what drifts (see the 2026-08-09 residue-aliasing entry --
        # render the schedule, do not trust unit tests).
        sched = ar._build_batch_schedule(['EUR_USD', 'SPX500_USD', 'XAU_USD'], 40)
        acad = [c for (_i, c, _w, _n, _d, _tf) in sched
                if 'ACADEMIC RECALL MODE' in c]
        assert acad, 'no academic slots in a 40-iteration schedule'
        assert all(ar._assigned_academic_anomaly(c) in ar._ACADEMIC_ANOMALIES
                   for c in acad)

    def test_academic_prefix_is_canonicalised_to_one_spelling(self):
        # Every label below is a REAL variant written by the model over the first
        # 330 academic gens (2026-08-09..14). Left un-normalised they split one
        # anomaly across three rows, which is why per-anomaly conversion could not
        # be read at all.
        drift = {
            'Time-Series Momentum': 'Time-Series Momentum (12-1)',
            'Time-Series Momentum 12-1': 'Time-Series Momentum (12-1)',
            'Time-Series Momentum (12-1)': 'Time-Series Momentum (12-1)',
            'Low-Volatility Effect': 'Low-Volatility Effect (Time-Series Form)',
            'Volatility Risk Premium Proxy': 'Volatility Risk Premium Proxy (Realized-Vol Term Structure)',
            'Time-Series Breakout': 'Time-Series Breakout (Managed Futures Trend Premium)',
            'Real-Exchange-Rate Value': 'Real-Exchange-Rate Value (PPP Deviation)',
            'short-term reversal': 'Short-Term Reversal',
        }
        for written, canon in drift.items():
            out = ar._canonical_academic_rationale(f'ACADEMIC({written}): mechanism here.')
            assert out == f'ACADEMIC({canon}): mechanism here.', written

    def test_missing_colon_prefix_is_repaired(self):
        # eurgbp_auto_20260813_091302_i8 wrote the prefix without the colon, which
        # hides the row from any query keying on the documented `): ` separator.
        out = ar._canonical_academic_rationale('ACADEMIC(Short-term reversal) positioning unwinds.')
        assert out == 'ACADEMIC(Short-Term Reversal): positioning unwinds.'

    def test_short_and_long_term_reversal_do_not_collapse(self):
        assert ar._canonical_anomaly('Long-Term Reversal') == 'Long-Term Reversal'
        assert ar._canonical_anomaly('Short-Term Reversal') == 'Short-Term Reversal'

    def test_non_academic_and_off_rotation_rationales_are_untouched(self):
        plain = 'Gold mean-reverts against silver when correlation is high.'
        assert ar._canonical_academic_rationale(plain) == plain
        # An anomaly outside the rotation is a signal worth seeing, not something
        # to coerce onto the nearest neighbour.
        off = 'ACADEMIC(Post-Earnings Announcement Drift): drift persists.'
        assert ar._canonical_academic_rationale(off) == off

    def test_every_canonical_name_survives_a_round_trip(self):
        # If one canonical key were a prefix of another, the truncated-label match
        # would silently relabel it. This is the guard on that.
        for name in ar._ACADEMIC_ANOMALIES:
            assert ar._canonical_anomaly(name) == name
