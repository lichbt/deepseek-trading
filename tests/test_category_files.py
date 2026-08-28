"""Tests for categories/<name>.md — the single-source per-category instruction files.

Each generation category (macro, calendar, event, pair, asset, standard, wild) has
one md with a ## CONSTRAINT block (the injected text) + a ## GUIDANCE block (spliced
into the thesis prompt). The loader must be byte-identical to the inline fallback so
the refactor changed NOTHING the model sees, and must fail soft when a file is absent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar

CATEGORIES = ['macro', 'calendar', 'event', 'pair', 'asset', 'standard', 'wild', 'gap']


class TestCategoryFiles:
    def test_all_category_files_present_and_parsed(self):
        for name in CATEGORIES:
            cat = ar._load_category(name)
            assert cat is not None, f"categories/{name}.md missing or unparseable"
            assert cat['constraint'], f"{name}.md has empty ## CONSTRAINT"
            assert cat['guidance'], f"{name}.md has empty ## GUIDANCE"

    def test_static_constraints_byte_identical_to_fallback(self):
        # Static categories: the md CONSTRAINT must equal the inline fallback exactly.
        for name in ('calendar', 'event', 'wild', 'pair', 'gap'):
            assert ar._category_constraint(name) == ar._FALLBACK_CONSTRAINTS[name], name

    def test_standard_list_matches_fallback(self):
        assert ar._category_list('standard') == ar._FALLBACK_STANDARD

    def test_creative_rotation_assembled_correctly(self):
        # Public rotation = standard items + the pair constraint, pair last.
        assert ar._CREATIVE_CONSTRAINTS == ar._FALLBACK_STANDARD + [ar._FALLBACK_PAIR]
        assert 'Cross-market PAIR' in ar._CREATIVE_CONSTRAINTS[-1]
        assert len(ar._CREATIVE_CONSTRAINTS) == 10

    def test_macro_dynamic_tokens_fill(self):
        out = ar._macro_constraint_for('EUR_USD')
        assert '{instrument}' not in out and '{cols}' not in out
        assert 'EUR_USD' in out and 'macro-archetype' in out

    def test_asset_dynamic_tokens_fill_and_keep_literal_braces(self):
        out = ar._asset_mode_for('XAU_USD', seed=1)
        assert out is not None
        assert '{instrument}' not in out and '{chosen}' not in out
        # literal brace list is NOT a token — must survive verbatim
        assert '{open, high, low, close, date}' in out

    def test_fallback_when_file_missing(self, monkeypatch, tmp_path):
        # Point the loader at an empty dir and clear the cache: it must fall back
        # to the inline constants, NOT crash or return ''.
        monkeypatch.setattr(ar, '_CATEGORY_DIR', tmp_path)
        monkeypatch.setattr(ar, '_CATEGORY_CACHE', {})
        assert ar._category_constraint('calendar') == ar._FALLBACK_CONSTRAINTS['calendar']
        assert ar._category_constraint('event') == ar._FALLBACK_CONSTRAINTS['event']
        assert ar._category_constraint('gap') == ar._FALLBACK_CONSTRAINTS['gap']
        assert ar._category_list('standard') == ar._FALLBACK_STANDARD
        assert '{instrument}' not in ar._macro_constraint_for('EUR_USD')  # tokens still fill

    def test_thesis_prompt_splices_all_four_sections(self):
        rules = ar._get_thesis_rules()
        assert '<!-- CATEGORY_GUIDANCE' not in rules      # sentinel replaced
        for h in ('## Macro data', '## Cross-market', '## Economic-event data',
                  '## Calendar / seasonal', '## Microstructure data'):
            assert rules.count(h) == 1, f"{h} appears {rules.count(h)}x"


class TestGapCategory:
    """The gap category encodes two MEASURED facts (2026-08-27, OANDA daily bars
    2015-01-01..2026-08-25, 31 instruments). Both are the kind of thing a later
    edit quietly softens into folklore, so pin them."""

    def test_constraint_forbids_the_unreachable_same_bar_fill(self):
        # compute_returns_with_stop enters at close[i-1], so the gap bar's own
        # open->close fill cannot be traded. The constraint must say so, because
        # "the gap fills during the day" is the thesis the model reaches for first.
        c = ar._category_constraint('gap')
        assert 'NOT capturable' in c
        assert 'entry is at the CLOSE of the signal bar' in c
        assert 'earns bar t+1' in c
        # ...but exiting AT the prior close IS reachable, and the constraint must
        # say so or the model drops the whole gap-fill exit family.
        assert 'Exiting AT the prior close is fine' in c

    def test_constraint_forbids_the_unconditional_form(self):
        # Measured ~0.02 ATR on the tradeable leg, at or under round-trip cost.
        c = ar._category_constraint('gap')
        assert 'No unconditional fade or continuation' in c
        assert 'SIGN SPLITS BY MECHANISM' in c
        assert 'GAP-UP and GAP-DOWN must BOTH trade' in c

    def test_constraint_computes_the_gap_rather_than_naming_a_column(self):
        # There is no injected `gap` column; inventing one fails at signal-check,
        # the same way cot_report_change did for the asset category.
        c = ar._category_constraint('gap')
        assert "df['open'] - df['close'].shift(1)" in c
        assert 'never invent a `gap` column' in c

    def test_gap_guidance_is_not_spliced_into_the_shared_prompt(self):
        # It is documentation for maintainers, not prompt text. Splicing it added
        # 977 tokens to EVERY thesis chunk's system prompt and blew the 12,000
        # guardrail (a real batch hit ~12,603 and returned 4/20 theses instead of
        # the usual 19-20/20). The gap family's operational content lives in its
        # CONSTRAINT, which is injected once, on the gap slot alone.
        rules = ar._get_thesis_rules()
        assert '## Overnight / weekend gaps' not in rules
        assert ar._category_guidance('gap')          # but the block still exists

    def test_thesis_prompt_fits_the_generation_guardrail(self):
        # _generate_candidate refuses any prompt over 12,000 estimated tokens.
        # Measured production maximum across the last 8 batches was 11,767, i.e.
        # 233 tokens of headroom, and NOTHING was watching it. This is the guard:
        # the shared system rules must leave room for the per-batch user message
        # (schedule + failure context + directives), which is the variable part.
        assert ar._estimate_tokens(ar._get_thesis_rules()) < 7800

    def test_constraint_pins_the_size_calibration(self):
        # A fixed ATR multiple is the family's signature failure: |gap_atr| > 1.5
        # is 4 bars in 2,996 on NATGAS, and the first thesis this category ever
        # generated used exactly that.
        c = ar._category_constraint('gap')
        assert 'NEVER A FIXED ATR MULTIPLE' in c
        assert 'FOUR BARS IN ELEVEN YEARS' in c
        assert 'quantile(0.8)' in c

    def test_constraint_warns_against_stacking_selective_conditions(self):
        # Measured on the first real batch (2026-08-27): the percentile rule cured
        # the fixed-ATR-multiple starvation, but percentile AND still-unfilled AND
        # a vol regime still produced "only 4 signals across all param combos" on
        # BCO_USD. academic.md already carries this clause; gap needs it too.
        c = ar._category_constraint('gap')
        assert 'SIGNAL STARVATION' in c
        assert 'pick ONE conditioning axis' in c

    def test_constraint_allows_the_exit_state_its_own_exits_require(self):
        # THE DEFECT THIS PINS: the hard-limits line was copied from the standard
        # constraint and banned "per-bar loops or entry-price tracking" — which
        # forbids exactly the gap-fill exits the same constraint MANDATES.
        # codegen.md:187 explicitly prescribes one stateful single pass for exit
        # state; the real ban is on re-implementing the ATR stop.
        c = ar._category_constraint('gap')
        assert 'EXIT STATE IS ALLOWED' in c
        assert 'ONE stateful single pass' in c
        assert 'ENTRY and FILTER must be vectorized' in c
        assert 'compute_returns_with_stop' in c

    def test_constraint_names_the_real_directional_bias_gate(self):
        # validator.py:775 rejects long fraction > 60% or structurally one-sided.
        # The constraint states the number so the thesis pre-empts the gate
        # instead of discovering it after codegen.
        c = ar._category_constraint('gap')
        assert 'long more than 60% of its bars' in c

    def test_constraint_says_macro_columns_are_absent(self):
        c = ar._category_constraint('gap')
        assert 'macro columns' in c and 'NOT available' in c

    def test_guidance_records_why_gap_is_daily_only(self):
        # Measured, so an "intraday gaps are richer" proposal is answered by data
        # rather than relitigated: intraday bars gap LESS and far smaller.
        g = ar._category_guidance('gap')
        assert 'Intraday bars gap LESS' in g
        assert 'fix_runner.py:433' in g

    def test_constraint_does_not_ask_for_an_invalid_strategy_family(self):
        # strategy_family is a CLOSED set (auto_research._VALID_FAMILIES); a
        # thesis declaring "gap" is discarded with 'unknown strategy_family'.
        # Attribution comes from the slot (persisted as strategies.slot_label),
        # never from this field.
        c = ar._category_constraint('gap')
        assert 'flow-proxy' in c and 'Do NOT write "gap"' in c
        assert 'gap' not in ar._VALID_FAMILIES

    def test_constraint_gates_the_spread_column_behind_its_archetype(self):
        # `spread` exists only under archetype "spread"; referencing it otherwise
        # fails at signal-check, the same way invented asset columns did.
        c = ar._category_constraint('gap')
        assert 'ONLY under archetype "spread"' in c

    def test_guidance_carries_the_date_stamp_trap(self):
        # OANDA stamps a daily bar at its OPEN, so dow==6 is Monday's session and
        # dow==4 is ~4 bars in 3,010. A thesis written on the intuitive labels
        # gets ~0 signals. This is the single most expensive trap in the family.
        g = ar._category_guidance('gap')
        assert '`dow == 6`' in g and "Monday's session" in g
        assert '`dow == 4`' in g and 'BARELY EXISTS' in g


class TestRedundantGateDeadlock:
    """The rubric rejects a filter the entry already implies; two categories asked for one.

    Measured on the 2026-08-27 night batch: CALENDAR generated 14 theses and the
    self-critique rejected all 14 — a 100% kill rate, zero reaching codegen — and
    every rejection was the same finding ("the regime gate is strictly implied by
    the entry conditions"). EVENT lost 8 of 11 the same way. The critic was right
    each time: prompts/self_critique_v4.txt makes it a mechanical test, and its
    own worked example is entry `days_to_event<=2` with filter `days_to_event<=3`.

    The defect was in the categories. calendar.md said "The calendar window IS the
    regime gate (no separate price detector needed)" and event.md allowed the
    event column in "ENTRY or FILTER", so the model put a calendar/event condition
    in both slots exactly as instructed and the critic killed it. asset.md and
    gap.md already carried the correct wording, which is why those two reached
    codegen. This pins the fix in both the file and its inline fallback.
    """

    def _both(self, name):
        # the .md the loader reads AND the inline constant it falls back to
        return [ar._category_constraint(name), ar._FALLBACK_CONSTRAINTS[name]]

    def test_calendar_no_longer_calls_the_window_its_regime_gate(self):
        for c in self._both('calendar'):
            assert 'IS the regime gate' not in c
            assert 'no separate price detector' not in c

    def test_calendar_demands_a_separate_regime_condition(self):
        for c in self._both('calendar'):
            assert 'ENTRY trigger' in c
            assert 'SEPARATE price/volatility regime condition' in c
            assert 'REJECTED' in c
            # the escape hatch the rubric itself names: repeat the window only
            # alongside a conjunct that does real work
            assert 'ONE conjunct' in c

    def test_event_puts_the_timing_in_the_entry_not_the_filter(self):
        for c in self._both('event'):
            assert 'ENTRY or FILTER' not in c
            assert 'The ENTRY MUST reference at least one of' in c
            assert 'ONE conjunct' in c
        # the schedule's is_event daily-pin greps for these literals — the
        # rewrite must not drop them (event.md header records this)
        for c in self._both('event'):
            assert 'days_to_event' in c and 'event_window' in c

    def test_the_gap_slot_residue_is_recorded_as_14_everywhere(self):
        # i%15==8 was superseded 2026-08-27 (its only hit in 1..20 is i=8, always
        # wild, so the family would have fired zero times), but two comments kept
        # asserting it. auto_research.py:2362 is the code and it says 14.
        src = (ar.__file__ or '').replace('.pyc', '.py')
        text = open(src).read()
        assert 'i % 15 == 14' in text
        gap_md = ar._CATEGORY_DIR / 'gap.md' if hasattr(ar._CATEGORY_DIR, '__truediv__') \
            else None
        if gap_md and gap_md.exists():
            head = gap_md.read_text().splitlines()[0]
            assert 'i%15==14' in head and 'i%15==8, ~6%' not in head
