"""steering.md — hand-editable generation knobs (2026-08-03).

The point of these tests is the FAIL-SOFT contract and the BOUNDS. This config
runs unattended overnight, so a typo must degrade to defaults rather than stop
generation, and the focus bias must never grow past its slot budget or eat the
wild-exploration floor.
"""
import textwrap

import pytest

import steering
import auto_research as ar


def _write(tmp_path, body: str):
    p = tmp_path / 'steering.md'
    p.write_text('# Steering\n\nprose\n\n```yaml\n' + textwrap.dedent(body) + '\n```\n')
    return p


# ---------------------------------------------------------------- parsing ---

class TestLoadIsAlwaysSafe:
    def test_missing_file_returns_defaults(self, tmp_path):
        s = steering.load(tmp_path / 'nope.md')
        assert s.focus_instruments == []
        assert s.timeframe_rotation == steering.DEFAULT_TIMEFRAME_ROTATION

    def test_no_yaml_block_returns_defaults(self, tmp_path):
        p = tmp_path / 'steering.md'
        p.write_text('# Steering\n\njust prose, no block\n')
        assert steering.load(p).timeframe_rotation == steering.DEFAULT_TIMEFRAME_ROTATION

    def test_malformed_yaml_returns_defaults_without_raising(self, tmp_path):
        p = _write(tmp_path, 'focus_instruments: [XAU_USD\n  bad: : :')
        s = steering.load(p)          # must not raise
        assert s.focus_instruments == []

    def test_yaml_block_that_is_not_a_mapping_returns_defaults(self, tmp_path):
        p = _write(tmp_path, '- just\n- a\n- list')
        assert steering.load(p).focus_instruments == []

    def test_empty_block_returns_defaults(self, tmp_path):
        p = _write(tmp_path, '')
        assert steering.load(p).timeframe_rotation == steering.DEFAULT_TIMEFRAME_ROTATION


class TestValidation:
    def test_symbols_are_upper_cased_and_stripped(self, tmp_path):
        p = _write(tmp_path, 'focus_instruments: [" xau_usd ", nzd_usd]')
        assert steering.load(p).focus_instruments == ['XAU_USD', 'NZD_USD']

    def test_a_bare_string_is_accepted_as_a_one_item_list(self, tmp_path):
        p = _write(tmp_path, 'focus_instruments: XAU_USD')
        assert steering.load(p).focus_instruments == ['XAU_USD']

    def test_avoid_beats_focus_for_an_instrument_in_both(self, tmp_path):
        # The risk-INCREASING reading of a contradictory config is to over-sample
        # something the user asked to remove, so it must lose.
        p = _write(tmp_path, 'focus_instruments: [XAU_USD, NZD_USD]\n'
                             'avoid_instruments: [XAU_USD]')
        s = steering.load(p)
        assert s.focus_instruments == ['NZD_USD']
        assert s.avoid_instruments == ['XAU_USD']

    @pytest.mark.parametrize('bad', ['0', '-3', 'true', '"ten"', '1.5'])
    def test_bad_focus_slot_every_falls_back(self, tmp_path, bad):
        p = _write(tmp_path, f'focus_slot_every: {bad}')
        assert steering.load(p).focus_slot_every == steering.DEFAULT_FOCUS_SLOT_EVERY

    def test_unknown_timeframes_are_dropped(self, tmp_path):
        p = _write(tmp_path, 'timeframe_rotation: [D, M5, H4, nonsense]')
        assert steering.load(p).timeframe_rotation == ['D', 'H4']

    def test_all_invalid_timeframes_falls_back_rather_than_emptying(self, tmp_path):
        p = _write(tmp_path, 'timeframe_rotation: [M5, M1]')
        assert steering.load(p).timeframe_rotation == steering.DEFAULT_TIMEFRAME_ROTATION

    def test_prose_mentioning_the_fence_inline_does_not_break_parsing(self, tmp_path):
        # Regression: the first steering.md described its own format in a
        # sentence containing the fence marker, so a naive find() started
        # mid-prose. The load then warned and fell back to defaults — the file
        # LOOKED fine and silently did nothing.
        p = tmp_path / 'steering.md'
        p.write_text(
            '# Steering\n\nEverything lives in the ```yaml block below.\n\n'
            '```yaml\nfocus_instruments: [XAU_USD]\n```\n'
        )
        assert steering.load(p).focus_instruments == ['XAU_USD']

    def test_unterminated_fence_returns_defaults(self, tmp_path):
        p = tmp_path / 'steering.md'
        p.write_text('# Steering\n\n```yaml\nfocus_instruments: [XAU_USD]\n')
        assert steering.load(p).focus_instruments == []

    def test_unknown_keys_are_ignored(self, tmp_path):
        p = _write(tmp_path, 'focus_instruments: [XAU_USD]\nmy_note: remember this')
        assert steering.load(p).focus_instruments == ['XAU_USD']


# --------------------------------------------------------------- schedule ---

INSTRUMENTS = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'XAU_USD', 'NZD_USD', 'BTC_USD']


def _sched(steer=None, n=40):
    return ar._build_batch_schedule(INSTRUMENTS, n, 0, exploit_pool=[], steer=steer)


class TestSchedulingIsUnchangedByDefault:
    def test_no_steering_argument_still_builds(self):
        assert len(_sched()) == 40

    def test_empty_steering_matches_the_no_argument_schedule(self):
        # Guards the migration: an unconfigured steering.md must not silently
        # change what gets generated.
        assert [x[0] for x in _sched()] == [x[0] for x in _sched(steering.Steering())]


class TestFocusIsBoundedAndNeverEatsTheWildFloor:
    def test_focus_instruments_get_extra_slots(self):
        s = steering.Steering(focus_instruments=['XAU_USD'], focus_slot_every=10)
        insts = [x[0] for x in _sched(s)]
        plain = [x[0] for x in _sched()]
        assert insts.count('XAU_USD') > plain.count('XAU_USD')

    def test_focus_share_stays_within_its_slot_budget(self):
        # every=10 over 40 iterations => at most 4 focus slots. A bias, not a
        # takeover: chasing recent winners is an overfit pressure and the bound
        # is what keeps it honest.
        s = steering.Steering(focus_instruments=['XAU_USD'], focus_slot_every=10)
        insts = [x[0] for x in _sched(s)]
        rotation_hits = [x[0] for x in _sched()].count('XAU_USD')
        assert insts.count('XAU_USD') <= rotation_hits + 4

    def test_wild_slots_are_never_reassigned_to_focus(self):
        s = steering.Steering(focus_instruments=['XAU_USD'], focus_slot_every=2)
        plain = _sched()
        focused = _sched(s)
        for a, b in zip(plain, focused):
            if a[2]:                      # wild flag
                assert b[2] and a[0] == b[0], 'a wild slot was hijacked'

    def test_empty_focus_pool_changes_nothing(self):
        s = steering.Steering(focus_instruments=[], focus_slot_every=2)
        assert [x[0] for x in _sched(s)] == [x[0] for x in _sched()]

    def test_multiple_focus_instruments_share_the_slots_round_robin(self):
        s = steering.Steering(focus_instruments=['XAU_USD', 'NZD_USD'],
                              focus_slot_every=5)
        insts = [x[0] for x in _sched(s)]
        assert insts.count('XAU_USD') >= 2 and insts.count('NZD_USD') >= 2


class TestAvoidAndTimeframe:
    def test_avoided_instrument_leaves_the_rotation(self):
        s = steering.Steering(avoid_instruments=['BTC_USD'])
        assert 'BTC_USD' not in [x[0] for x in _sched(s)]

    def test_schedule_length_is_preserved_when_avoiding(self):
        # Dropping from the pool rather than skipping slots — a skip would
        # silently shorten the batch.
        s = steering.Steering(avoid_instruments=['BTC_USD', 'EUR_USD'])
        assert len(_sched(s)) == 40

    def test_avoiding_everything_is_refused_rather_than_emptying_the_pool(self):
        s = steering.Steering(avoid_instruments=[i.upper() for i in INSTRUMENTS])
        assert len(_sched(s)) == 40      # falls back to the full pool

    def test_timeframe_rotation_is_honoured(self):
        s = steering.Steering(timeframe_rotation=['D'])
        tfs = {x[5] for x in _sched(s)}
        assert tfs <= {'D', None}, tfs   # None = wild (picks its own)

    def test_h1_is_absent_from_the_shipped_default(self):
        # H1 validates at 0.047% vs D 0.291% / H4 0.258% — the default drops H1
        # and KEEPS H4 rather than dropping all intraday.
        assert 'H1' not in steering.DEFAULT_TIMEFRAME_ROTATION
        assert 'H4' in steering.DEFAULT_TIMEFRAME_ROTATION


class TestShippedFileIsValid:
    def test_repo_steering_md_parses(self):
        s = steering.load()              # the real steering.md
        assert s.focus_slot_every >= 1
        assert s.timeframe_rotation
        assert all(tf in {'M30', 'H1', 'H4', 'D', 'W'} for tf in s.timeframe_rotation)


# --------------------------------------------------------- bucket weights ---
#
# Added 2026-10-09 with the knob itself. The invariants that matter: the default
# reproduces the equal deal EXACTLY (so nothing changes until the weights are
# hand-edited), every category keeps at least one slot (or its pass rate stops
# being measurable — the whole point of the equal deal), and a typo degrades to
# defaults instead of stopping generation.

class TestBucketWeights:
    def test_default_is_the_equal_deal(self):
        s = steering.Steering()
        eq = (list(ar._EQUAL_BUCKETS) * (31 // len(ar._EQUAL_BUCKETS))
              + ['wild'] * (31 % len(ar._EQUAL_BUCKETS)))
        assert ar._weighted_deal(31, s.bucket_weights) == eq

    def test_unknown_bucket_and_bad_value_are_dropped(self, tmp_path):
        p = _write(tmp_path, '''
            bucket_weights:
              macro: 6
              bogus: 4
              pair: not-a-number
        ''')
        w = steering.load(p).bucket_weights
        assert w['macro'] == 6
        assert 'bogus' not in w
        assert w['pair'] == steering.DEFAULT_BUCKET_WEIGHTS['pair']

    def test_a_zero_weight_is_raised_so_the_family_stays_measured(self, tmp_path):
        p = _write(tmp_path, 'bucket_weights:\n  asset: 0\n  macro: 6\n')
        assert steering.load(p).bucket_weights['asset'] == 1

    def test_weights_are_relative_not_slot_counts(self):
        deal = ar._weighted_deal(31, {'macro': 6, 'pair': 4})
        assert len(deal) == 31
        # omitted buckets default to 1, so the share is relative to the SUM of
        # weights, not to the number of buckets named
        assert deal.count('macro') > deal.count('pair') > 1
        assert deal.count('asset') >= 1          # never starved

    def test_every_bucket_gets_a_slot_in_a_full_batch(self):
        deal = ar._weighted_deal(31, {'macro': 9})
        assert set(deal) == set(ar._EQUAL_BUCKETS)

    def test_short_and_long_batches_are_exact_lengths(self):
        for n in (1, 7, 31, 200):
            assert len(ar._weighted_deal(n, {'macro': 3, 'gap': 1})) == n
