"""Tests for _build_batch_schedule — the explore/exploit balance of batch gen.

Data-driven 'exploit' slots steer toward demonstrated-edge-but-DD-blocked
families, but they must stay a BOUNDED minority on top of the random rotation,
never displacing the wild (pure-exploration) slots — so data-driven focus can't
collapse diversity.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar

INSTS = ['EUR_USD', 'XAU_USD', 'GBP_JPY', 'SPX500_USD', 'NAS100_USD', 'XCU_USD']
POOL = ['WTICO_USD', 'ETH_USD', 'XAG_USD']


def _production_max_iter() -> int:
    """MAX_ITER as run_forever.sh actually sets it.

    Read, never hardcoded: `i` restarts at 1 every batch and the deal hands out
    max_iterations // 10 slots to each of the ten categories/*.md files, with the
    remainder ALL going to wild — so MAX_ITER decides each family's share (31 -> 3
    each and 4 for wild; 20 -> 2 each and 2 for wild; 25 -> 2 each and 7 for wild).
    A test that pins the batch length silently stops describing production the
    moment it moves.
    """
    import re
    from pathlib import Path
    sh = (Path(__file__).resolve().parent.parent / 'run_forever.sh').read_text()
    m = re.search(r'^MAX_ITER=(\d+)', sh, re.M)
    assert m, 'MAX_ITER not found in run_forever.sh'
    return int(m.group(1))


def _exploit(sch):
    return [s for s in sch if 'DATA-DRIVEN' in s[1]]


def _wild(sch):
    return [s for s in sch if s[2]]


def _event(sch):
    return [s for s in sch if 'days_to_event' in s[1] or 'event_window' in s[1]]


def _calendar(sch):
    # startswith, not equality: the calendar slot carries a per-visit pinned
    # mechanism appended to the category text (see _calendar_mode_for).
    return [s for s in sch if s[1].startswith(ar._CALENDAR_CONSTRAINT)]


class TestBatchSchedule:
    def test_exploit_bounded_and_never_wild(self):
        # Explicit offsets: without them this reads the PERSISTENT walks and the
        # assertion depends on whatever the last production batch left behind.
        sch = ar._build_batch_schedule(INSTS, 31, 0, POOL, academic_offset=0,
                                       creative_offset=0, macro_offset=0, pair_offset=0)
        ex = _exploit(sch)
        assert 0 < len(ex) <= 31 // ar.EXPLOIT_SLOT_EVERY + 1
        assert all(not s[2] for s in ex)        # an exploit slot is never a wild slot
        assert len(ex) / 31 < 0.15              # exploration stays the backbone

    def test_wild_floor_preserved(self):
        """The exploration floor survives the equal deal. WILD is one of the ten
        files and takes the remainder slot, so it holds 4 of 31 (12.9%) instead of
        the old every-8th 3 — the extra slot can only add exploration."""
        sch = ar._build_batch_schedule(INSTS, 31, 0, POOL)
        w = _wild(sch)
        assert [s[3] for s in w] == [10, 20, 30, 31]     # dealt, plus the remainder
        assert all('WILD' in s[1] for s in w)            # pure exploration, not exploit

    def test_exploit_uses_pool_and_risk_instruction(self):
        for s in _exploit(ar._build_batch_schedule(INSTS, 31, 0, POOL)):
            assert s[0] in POOL                           # targets a DD-blocked family
            assert 'drawdown control' in s[1].lower()     # carries the risk-control steer
            assert s[5] == 'D'                            # exploit designs on daily

    def test_empty_pool_is_pure_rotation(self):
        sch = ar._build_batch_schedule(INSTS, 31, 0, exploit_pool=[])
        assert _exploit(sch) == []                        # fail-soft: no exploit slots
        assert len(_wild(sch)) == 4                       # rotation backbone intact

    def test_none_pool_is_failsoft(self):
        sch = ar._build_batch_schedule(INSTS, 20, 0, exploit_pool=None)
        assert _exploit(sch) == [] and len(sch) == 20

    def test_schedule_length_matches_iterations(self):
        assert len(ar._build_batch_schedule(INSTS, 20, 0, POOL)) == 20

    def test_cadence_keeps_exploration_dominant(self):
        assert ar.EXPLOIT_SLOT_EVERY >= 10                # exploit is a small minority

    def test_event_slot_has_measured_presence(self):
        # Event was 1-of-N creative constraints the model ignored ~90% of the time.
        # Under the equal deal it is one of ten files, so its share is exactly 10%.
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        ev = _event(sch)
        assert 0.09 < len(ev) / len(sch) < 0.11          # one of ten equal files

    def test_event_slots_daily_pinned(self):
        # days_to_event / event_window are day-resolution — meaningless on weekly bars.
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        assert all(s[5] == 'D' for s in _event(sch))     # every event slot forced to daily

    def test_event_never_collides_with_calendar(self):
        sch = ar._build_batch_schedule(INSTS, 2000, 0, POOL)
        ev_i = {s[3] for s in _event(sch)}
        cal_i = {s[3] for s in _calendar(sch)}
        assert ev_i and cal_i                            # both families present
        assert ev_i.isdisjoint(cal_i)                    # offset moduli never overlap

    def test_event_constraint_self_enforces(self):
        # The forced constraint must name the exact injected columns and threaten discard,
        # so the model actually references them instead of falling back to price-only.
        c = ar._EVENT_CONSTRAINT
        assert 'days_to_event' in c and 'event_window' in c and 'days_since_event' in c
        assert 'DISCARD' in c.upper()

    def test_event_removed_from_creative_rotation(self):
        # Single source of truth: event lives only in the forced slot now.
        assert not any('days_to_event' in c or 'event_window' in c
                       for c in ar._CREATIVE_CONSTRAINTS)

    def test_exploit_instruments_gated_by_flag(self, monkeypatch):
        # A 'NORMAL' (baseline) batch must get NO exploit slots regardless of the DB.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', False)
        assert ar._exploit_instruments() == []
        # A 'DRIVEN' batch pulls the DD-blocked families.
        monkeypatch.setattr(ar, 'EXPLOIT_ENABLED', True)
        import meta_review
        monkeypatch.setattr(meta_review, 'dd_blocked_instruments', lambda *a, **k: ['WTICO_USD'])
        assert ar._exploit_instruments() == ['WTICO_USD']


class TestRotationAliasing:
    """Residue classes colliding with rotation lengths silently starve the pool.

    Found 2026-08-09 by RENDERING a schedule rather than trusting a unit test —
    three separate families were affected, and every unit test passed throughout.
    """

    def _sched(self, n=3000):
        import steering
        return ar._build_batch_schedule(list(INSTS), n, steer=steering.Steering())

    def _creative(self, sch):
        # Via _slot_label, not `c in cc`: the pair item (CREATIVE[9]) carries a
        # per-visit mechanism pin, so its text no longer compares equal to the
        # bare constraint. The shared helper also exercises that mapping.
        out = []
        for _, c, w, _, _, tf in sch:
            label = ar._slot_label(c, w)
            if label.startswith('CREATIVE['):
                out.append((int(label[len('CREATIVE['):-1]), tf))
        return out

    def test_every_creative_constraint_is_reachable(self):
        """Indexed by i, calendar owned i%10==0 and event owned i%10==5 and both
        outrank the creative branch — so constraints 0 and 5 could NEVER be
        scheduled. Dead since the event slot landed 2026-07-08.
        The pair item is no longer one of these: it is its own dealt bucket and
        labels itself PAIR, so the reachable set is the nine standard items."""
        seen = {j for j, _ in self._creative(self._sched())}
        missing = set(range(len(ar._STANDARD_CONSTRAINTS))) - seen
        assert not missing, f'unreachable creative constraints: {sorted(missing)}'
        assert max(seen) == len(ar._STANDARD_CONSTRAINTS) - 1

    def test_constraint_is_not_welded_to_one_timeframe(self):
        """Constraint index ran off i%10 and timeframe off (i-1)%10 — same
        modulus, locked one apart — so each constraint saw exactly ONE timeframe
        for the life of the pipeline."""
        import steering
        from collections import defaultdict
        pairs = defaultdict(set)
        for j, tf in self._creative(self._sched()):
            pairs[j].add(tf)
        distinct = len(set(steering.Steering().timeframe_rotation))
        for j, tfs in pairs.items():
            if j == ar._CREATIVE_CONSTRAINTS.index(
                    next(c for c in ar._CREATIVE_CONSTRAINTS if 'day-of-week' in c)):
                continue          # deliberately day-pinned, see below
            assert len(tfs) > 1, f'creative constraint {j} welded to {tfs}'
            assert len(tfs) == distinct, f'constraint {j} reaches only {sorted(tfs)}'

    def test_weekly_reaches_the_creative_pool(self):
        """Weekly sat at rotation index 9, which paired with the one constraint
        calendar had already claimed — so W never reached a creative slot."""
        assert any(tf == 'W' for _, tf in self._creative(self._sched()))

    def test_day_of_week_constraint_stays_daily(self):
        """Decoupling the timeframe freed this constraint to reach WEEKLY, where
        'day-of-week' is not addressable — the degeneracy that failed the event
        family at 191 gens / 0 passes. It must stay day-resolution."""
        for _, c, _, i, _, tf in self._sched():
            if 'day-of-week' in c or 'time-of-session' in c:
                assert tf == 'D', f'day-of-week constraint on {tf} at i={i}'

    def test_nnfx_timeframe_is_explicit_not_accidental(self):
        """The nnfx branch read as a rotation but every i%40==7 gave the same
        index, so it was constant 'D' while looking rotated."""
        tfs = {tf for _, c, _, _, _, tf in self._sched()
               if c.startswith(ar._NNFX_CONSTRAINT[:40])}
        assert tfs == {'D'}, tfs


class TestSlotLabel:
    """The run loop recomputed its log label from the iteration number, which is
    only correct for the per-iteration fallback. When the thesis came from the
    BATCH, every forced slot logged as a creative constraint — an academic 12-1
    momentum thesis printed as '[constraint[1]]' — so the log could not attribute
    a failure to a family. Observed 2026-08-09 in a live run."""

    def test_each_family_labels_itself(self):
        import steering
        seen = {}
        for inst, c, wild, i, _, _ in ar._build_batch_schedule(
                list(INSTS), 500, steer=steering.Steering()):
            seen.setdefault(ar._slot_label(c, wild), 0)
            seen[ar._slot_label(c, wild)] += 1
        for family in ('WILD', 'MACRO', 'CALENDAR', 'EVENT', 'NNFX', 'ACADEMIC'):
            assert family in seen, f'{family} never labelled; got {sorted(seen)}'

    def test_academic_is_not_labelled_creative(self):
        import steering
        acad = ar._category_constraint('academic', anomaly='', instrument='', cols='')[:40]
        for inst, c, wild, i, _, _ in ar._build_batch_schedule(
                list(INSTS), 500, steer=steering.Steering()):
            if c.startswith(acad):
                assert ar._slot_label(c, wild) == 'ACADEMIC', \
                    f'i={i} academic slot labelled {ar._slot_label(c, wild)}'

    def test_creative_label_carries_its_real_index(self):
        for j, c in enumerate(ar._STANDARD_CONSTRAINTS):
            assert ar._slot_label(c) == f'CREATIVE[{j}]'
        # pair.md's item is still the last entry of _CREATIVE_CONSTRAINTS — the
        # creative LIST is where the loader keeps it — but it is a dealt bucket of
        # its own and labels itself PAIR, so a per-family query can ask for the
        # cross-market family without dragging the nine archetypes along.
        assert ar._CREATIVE_CONSTRAINTS[-1] == ar._PAIR_CONSTRAINT
        assert ar._slot_label(ar._PAIR_CONSTRAINT) == 'PAIR'
        assert ar._slot_label(ar._pair_mode_for(0)) == 'PAIR'
        assert len(ar._CREATIVE_CONSTRAINTS) == len(ar._STANDARD_CONSTRAINTS) + 1


class TestGapSlot:
    """The GAP bucket, one of the ten categories/*.md files in the equal deal.

    THE ONE THING TO KNOW: `i` restarts at 1 every batch, so a family's real rate is
    what the DEAL assigns it in one batch (run_forever.sh:8) — at 31 slots the ten
    files get 3 each and wild takes the 31st. The family used to be a residue of `i`
    (i%15==14, and before that i%15==8, whose only hit inside 1..20 was i=8 — always
    wild — so it rendered a healthy 5.83% over 3,000 slots and fired ZERO times in
    production). The deal deleted that whole class of bug: a slot exists because it
    was dealt, not because a modulus landed in range. MAX_ITER is read from
    run_forever.sh rather than hardcoded, because it decides the deal's shape.
    """
    PROD = _production_max_iter()

    def _sch(self, n=None):
        return ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL), n or self.PROD, 0,
            exploit_pool=[], academic_offset=0)

    def _gap(self, sch):
        return [s for s in sch if 'GAP MODE' in s[1]]

    def test_gap_fires_in_a_real_production_batch(self):
        g = self._gap(self._sch())
        expected = [8, 18, 28]      # gap's dealt slot at 31
        assert [s[3] for s in g] == expected, f'gap fired at {[s[3] for s in g]}'
        assert all(s[5] == 'D' for s in g)
        assert not any(s[2] for s in g)           # never a wild slot

    def test_gap_takes_its_slot_from_the_deal_only(self):
        # Every family's indices are disjoint by construction now, so the live
        # property is that gap holds exactly the slots the deal gave it and never
        # one that belongs to another family or to wild.
        sch = self._sch()
        lab = [ar._slot_label(c, w) for _, c, w, _, _, _ in sch]
        gap_i = {s[3] for s in self._gap(sch)}
        assert gap_i, 'gap never fires — the deal does not reach this batch length'
        for fam in ('MACRO', 'ACADEMIC', 'WILD', 'CALENDAR', 'EVENT', 'NNFX', 'ASSET', 'PAIR'):
            owned = {s[3] for s in sch if ar._slot_label(s[1], s[2]) == fam}
            assert not (gap_i & owned), fam
        assert lab.count('GAP') == len(gap_i)

    def test_gap_slot_labels_itself(self):
        # Not listed in _slot_label => falls through to the terminal fallback and
        # every gap line in the batch log names the wrong family.
        sch = self._sch()
        assert all(ar._slot_label(s[1], s[2]) == 'GAP' for s in self._gap(sch))

    def test_a_family_walk_stays_coprime_to_the_instrument_pool(self):
        # The deal repeats its ten buckets every ten slots, so inside one batch a
        # family's slots sit 10 apart and walk the pool with STRIDE 10. A pool whose
        # length shared a factor with that stride would hand a family the same few
        # instruments inside one batch (at 20 instruments stride 10 gives 2 distinct
        # names, not 3). The production pool is 31, prime, so the stride cycles the
        # whole book; the per-batch random offset (_pool_offset) is what spreads the
        # families across the book between batches.
        from math import gcd
        assert gcd(10, len(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL)) == 1

    def test_gap_is_daily_at_every_batch_length(self):
        # A gap is open - close.shift(1), so the timeframe defines what the gap
        # IS: H1/H4 make it an intra-session artifact, W a single Sunday reprice.
        for n in (20, 31, 200):
            assert {s[5] for s in self._gap(self._sch(n))} == {'D'}


class TestAssetSlotRevived:
    """The asset slot was STRUCTURALLY DEAD from the day it was written until
    2026-08-27: it tested (i%9==0) while requiring `not macro`, and macro is
    (i%3==0) — every multiple of 9 is a multiple of 3, so the conjunction was
    unsatisfiable. Rendered over 100,000 iterations it produced zero slots."""

    def _asset(self, sch):
        return [s for s in sch if s[1].startswith('ASSET MODE')]

    def test_asset_now_fires_in_a_real_production_batch(self):
        # Production is 31 slots with i restarting at 1 (run_forever.sh MAX_ITER),
        # so the deal gives asset three of them; this pins the 20-slot shape too,
        # because family shares are a function of MAX_ITER.
        pool = ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL
        sch = ar._build_batch_schedule(list(pool), 20, 0, exploit_pool=[], academic_offset=0)
        a = self._asset(sch)
        assert len(a) == 2, f'asset fired {len(a)}x in a 20-slot batch'
        assert [s[3] for s in a] == [2, 12] and all(s[5] == 'D' for s in a)

    def test_asset_does_not_eat_academic_or_gap(self):
        # The deal gives every family its own indices, so the three can never
        # collide. The old congruence chain had to prove this by arithmetic
        # (asset i%18==4 vs academic i%6==1 vs gap i%15==14) and the rejected
        # alternative i%9==4 would have cut academic by a third.
        pool = ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL
        for n in (20, 18000):
            sch = ar._build_batch_schedule(list(pool), n, 0, exploit_pool=[], academic_offset=0)
            acad = [s for s in sch if 'ACADEMIC RECALL' in s[1]]
            gap = [s for s in sch if 'GAP MODE' in s[1]]
            assert acad and gap, n
            assert not (set(s[3] for s in acad) & set(s[3] for s in gap))

    def test_asset_is_daily_and_labels_itself(self):
        pool = ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL
        sch = ar._build_batch_schedule(list(pool), 2000, 0, exploit_pool=[], academic_offset=0)
        a = self._asset(sch)
        assert {s[5] for s in a} == {'D'}
        assert all(ar._slot_label(s[1], s[2]) == 'ASSET' for s in a)

    def test_unknown_constraint_is_not_labelled_asset(self):
        # The terminal fallback used to BE 'ASSET', which was harmless only while
        # the slot was dead; now it would attribute junk to a live family.
        assert ar._slot_label('something nobody scheduled') == 'UNKNOWN'

    def test_no_concept_selects_the_empty_weekday(self):
        # df['date'] is the bar's OPEN stamp, one session early, so day_of_week 4
        # has ~4 bars in 3,010 and 5 is empty on every non-crypto instrument.
        # Three concepts used to sit on ==4 and would have failed at IS=0.
        import re
        crypto = ('BTC_USD', 'ETH_USD', 'LTC_USD')
        for ins, concepts in ar._ASSET_MODE_CONCEPTS.items():
            if ins in crypto:
                continue
            for c in concepts:
                for v in re.findall(r'day_of_week\s*==\s*(\d)', c):
                    assert v not in ('4', '5'), f'{ins}: {c}'


class TestCreativeRotationPersists:
    """n_creative used to reset on every call. A 20-slot production batch holds
    only 3 creative slots, so indices 0,1,2 were the ONLY ones ever drawn — 7 of
    10 constraints, including CREATIVE[9] (the forced cross-market PAIR
    constraint), had not been scheduled since MAX_ITER went 31 -> 20 on
    2026-07-24. Same defect the academic walk already had a persistent file for.
    """
    PROD = _production_max_iter()

    def _batch(self, offset, steer=None):
        import steering
        return ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL), self.PROD, 0,
            exploit_pool=[], steer=steer or steering.load(),
            academic_offset=0, creative_offset=offset)

    def _creative(self, sch):
        """Creative slots as (index, text). Resolved via _slot_label, not equality:
        the pair item (CREATIVE[9]) carries a per-visit mechanism pin, so its text
        no longer equals the bare _PAIR_CONSTRAINT."""
        out = []
        for s in sch:
            label = ar._slot_label(s[1], s[2])
            if label.startswith('CREATIVE['):
                out.append((int(label[len('CREATIVE['):-1]), s[1]))
        return out

    def test_resetting_covers_only_the_head_of_the_list(self):
        # The defect, pinned: with the counter reset every batch, ten batches draw
        # exactly what one batch draws — the first N indices, forever, where N is
        # the number of creative slots the batch happens to hold.
        # Pin it on the STANDARD walk: the pair item is its own bucket now and
        # fires every batch regardless, so it can no longer express a head-only
        # walk.
        def ids(sch):
            return [ar._STANDARD_CONSTRAINTS.index(s[1]) for s in sch
                    if s[1] in ar._STANDARD_CONSTRAINTS]
        one = ids(self._batch(0))
        seen = {j for _ in range(10) for j in ids(self._batch(0))}
        assert seen == set(one) == set(range(len(one)))
        assert len(one) < len(ar._STANDARD_CONSTRAINTS), \
            'batch now holds the whole list; this defect is no longer expressible'


    def test_persisted_walk_covers_the_whole_list(self):
        n, seen = 0, set()
        for _ in range(10):
            cre = self._creative(self._batch(n))
            n += len(cre)
            seen |= {j for j, _ in cre}
        assert seen == set(range(len(ar._STANDARD_CONSTRAINTS))), sorted(seen)

    def test_the_pair_item_is_its_own_family_not_the_tenth_creative(self):
        # Until 2026-09-17 the cross-market item labelled itself CREATIVE[9], which
        # welded the pair family to the nine standard archetypes in every per-family
        # query. It is dealt its own three slots and pinned its own mechanism, so it
        # must also carry its own family name.
        sch = self._batch(0)
        pairs = [c for _, c, w, *_ in sch if ar._slot_label(c, w) == 'PAIR']
        assert len(pairs) == 3
        assert all('PAIR' in c for c in pairs)
        assert not [c for _, c, w, *_ in sch
                    if ar._slot_label(c, w).startswith('CREATIVE[')
                    and 'Cross-market PAIR' in c]

    def test_no_family_share_moves(self):
        # The creative branch is the `else`, so the walk changes WHICH constraint
        # a creative slot draws, never how many slots each family gets. Compared
        # against offset 0 rather than against literals, so it holds at any
        # MAX_ITER.
        from collections import Counter
        def shares(sch):
            return Counter(ar._slot_label(c, w).split('[')[0]
                           for _, c, w, _, _, _ in sch)
        base = shares(self._batch(0))
        n = 0
        for _ in range(6):
            sch = self._batch(n)
            n += len(self._creative(sch))
            assert shares(sch) == base

    def test_explicit_offset_keeps_the_function_pure(self, monkeypatch, tmp_path):
        # Passing creative_offset must NOT touch the persistent counter — that is
        # the contract every test in this file relies on for determinism.
        f = tmp_path / '.creative_rotation'
        monkeypatch.setattr(ar, '_CREATIVE_ROTATION_FILE', f)
        self._batch(4)
        assert not f.exists()

    def test_production_path_advances_by_slots_consumed(self, monkeypatch, tmp_path):
        # Never by a fixed stride: a stride against the list length is exactly the
        # residue aliasing that made half the list unreachable to begin with.
        import steering
        f = tmp_path / '.creative_rotation'
        monkeypatch.setattr(ar, '_CREATIVE_ROTATION_FILE', f)
        sch = ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL), self.PROD, 0,
            exploit_pool=[], steer=steering.load(), academic_offset=0)
        # The walk counts the STANDARD items it renders, not every constraint that
        # is labelled CREATIVE: the pair item has its own bucket now and its own tf
        # walk, so it is not this counter's business.
        consumed = len([s for s in sch if s[1] in ar._STANDARD_CONSTRAINTS])
        assert consumed > 0
        assert f.read_text().strip() == str(consumed)

    def test_offset_reader_is_failsoft(self, monkeypatch, tmp_path):
        f = tmp_path / '.creative_rotation'
        monkeypatch.setattr(ar, '_CREATIVE_ROTATION_FILE', f)
        assert ar._creative_rotation_offset() == 0        # missing
        f.write_text('not a number')
        assert ar._creative_rotation_offset() == 0        # corrupt
        f.write_text('-5')
        assert ar._creative_rotation_offset() == 0        # negative clamped


class TestFallbackSharesTheBatchSchedule:
    """run()'s per-iteration fallback used to re-derive its own slot (constraint /
    detector / timeframe / label) from a hand-copied chain that had silently
    diverged from _build_batch_schedule (nnfx i%40 vs i%12, creative keyed by a
    persistent counter vs by `iteration`). So a thesis regenerated under partial
    model failure was attributed to a slot that never ran, and the fallback's
    prompt disagreed with the batch's. run() now builds the schedule ONCE and the
    fallback reads the same tuple; these tests pin that equivalence and that the
    shared schedule is not double-counted against the rotation counters.
    """

    def _sched(self, **kw):
        import steering
        return ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL), 20, 0,
            exploit_pool=[], steer=steering.load(),
            academic_offset=0, creative_offset=0, **kw)

    def test_schedule_param_is_a_pure_injection(self, monkeypatch):
        # Passing schedule= must use it verbatim and NOT rebuild (which would
        # double-advance the persistent academic/creative walks). Rebuild only
        # when absent. Stub the parts of _generate_thesis_batch that reach the
        # network (fingerprint formatting) and the LLM, so this stays offline.
        import steering
        s_built = self._sched()
        calls = []
        real_build = ar._build_batch_schedule
        def spy(*a, **k):
            calls.append(k.get('academic_offset'))
            return real_build(*a, **k)
        monkeypatch.setattr(ar, '_build_batch_schedule', spy)
        monkeypatch.setattr(ar, '_fp_compact', lambda *a, **k: '')
        monkeypatch.setattr(ar, 'call_openrouter',
                            lambda **kw: {'success': False, 'error': 'x', 'candidate': None})
        ar._generate_thesis_batch(['EUR_USD'] * 20, 20, schedule=s_built)
        assert calls == [], 'explicit schedule= must bypass _build_batch_schedule'
        # and without it, the generator builds exactly once
        calls.clear()
        ar._generate_thesis_batch(['EUR_USD'] * 20, 20)
        assert len(calls) == 1

    def test_fallback_slot_equals_batch_slot(self):
        # The tuple run() hands the fallback (schedule[iteration-1]) is, by
        # construction, the batch schedule's own entry — so attribution and the
        # forced timeframe/detector can never disagree with what the batch
        # rendered. Pin the shape run() relies on: (inst, constraint, wild, i,
        # detector, tf).
        s = self._sched()
        for i, entry in enumerate(s, 1):
            assert len(entry) == 6
            inst, constraint, wild, slot_i, detector, tf = entry
            assert slot_i == i                       # 1-based iteration, as rendered
            assert inst == entry[0]
            # The very property that made the old chain drift: the fallback's
            # label comes from the SAME constraint string the batch rendered.
            assert ar._slot_label(constraint, wild) not in ('', None)


class TestSlotLabelPersisted:
    """Which generation category produced a row had NO durable record before
    2026-08-27. strategy_family is a closed 7-value set with no slot for a new
    category, and the academic experiment already proved a model-written
    rationale prefix is not a usable join key (80.7% agreement with the real
    draw over 765 gens). So the SCHEDULE's own label is persisted instead."""

    def test_every_batch_item_carries_its_label(self):
        import steering
        sch = ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL), 20, 0,
            exploit_pool=[], steer=steering.load(), academic_offset=0, creative_offset=0)
        labels = [ar._slot_label(c, w) for _, c, w, _, _, _ in sch]
        assert 'GAP' in labels and 'ASSET' in labels and 'ACADEMIC' in labels
        assert 'UNKNOWN' not in labels          # every slot resolves to a family

    def test_column_exists_and_round_trips(self, tmp_path, monkeypatch):
        import sqlite3
        import pipeline_utils as pu
        monkeypatch.setattr(pu, 'DB_PATH', tmp_path / 'probe.db')
        pu.init_db()
        cols = [c[1] for c in sqlite3.connect(str(pu.DB_PATH)).execute(
            "select * from pragma_table_info('strategies')")]
        assert 'slot_label' in cols
        pu.insert_strategy('probe_gap', 'fp1', 'code', {'a': [1]}, 'r', 'D',
                           instrument='NATGAS_USD', slot_label='GAP')
        pu.insert_strategy('probe_old', 'fp2', 'code', {'a': [1]}, 'r', 'D',
                           instrument='GBP_USD')      # caller that passes nothing
        rows = dict(sqlite3.connect(str(pu.DB_PATH)).execute(
            'select id, slot_label from strategies').fetchall())
        assert rows['probe_gap'] == 'GAP'
        assert rows['probe_old'] is None            # back-compatible, not ''


class TestEqualGeneration:
    """Every categories/*.md file gets the same slice of the batch (2026-09-16).

    Before this, per-family shares came out of a congruence chain and nobody had
    chosen them: macro held 9 of 31 slots and 73% of the passes, while academic,
    wild, gap, asset, nnfx and exploit produced 0 passes each on 189-979
    generations apiece. Equal slots are what make the pass RATE comparable.
    """

    FILES = ('macro', 'calendar', 'event', 'pair', 'asset', 'standard', 'wild',
             'gap', 'nnfx', 'academic')

    def _batch(self, n=None, pool_offset=0):
        import steering
        return ar._build_batch_schedule(
            list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL),
            n or _production_max_iter(), pool_offset, exploit_pool=[],
            academic_offset=0, creative_offset=0, macro_offset=0, pair_offset=0,
            steer=steering.Steering())

    @staticmethod
    def _share(sch):
        """File -> slots. pair labels itself PAIR since 2026-09-17; before that it
        was CREATIVE[9], because _slot_label attributes by constraint TEXT and the
        pair item is the last entry of _CREATIVE_CONSTRAINTS. Rows generated before
        that date still carry CREATIVE[9] in strategies.slot_label, so an era query
        has to accept both spellings.
        """
        labs = [ar._slot_label(c, w) for _, c, w, _, _, _ in sch]
        return {
            'macro': labs.count('MACRO'),
            'calendar': labs.count('CALENDAR'),
            'event': labs.count('EVENT'),
            'pair': labs.count('PAIR'),
            'asset': labs.count('ASSET'),
            'standard': len([l for l in labs if l.startswith('CREATIVE[')]),
            'wild': labs.count('WILD'),
            'gap': labs.count('GAP'),
            'nnfx': labs.count('NNFX'),
            'academic': labs.count('ACADEMIC'),
        }

    def test_one_bucket_per_category_file(self):
        # A new categories/*.md file that nobody added to _EQUAL_BUCKETS would be
        # generated ZERO times, silently — the way the asset and gap slots spent
        # months producing nothing.
        assert {p.stem for p in ar._CATEGORY_DIR.glob('*.md')} == set(ar._EQUAL_BUCKETS)

    def test_every_file_gets_the_same_number_of_slots(self):
        share = self._share(self._batch())
        assert set(share) == set(self.FILES), share
        # 31 = 3 * 10 + 1, and the remainder is the exploration floor's.
        assert {k: v for k, v in share.items() if k != 'wild'} == \
            {k: 3 for k in share if k != 'wild'}, share
        assert share['wild'] == 4, share

    def test_shares_hold_at_other_batch_lengths(self):
        # i restarts at 1 every batch, so a share is a property of MAX_ITER: the
        # schedule this pipeline runs is the same slots every time.
        for n in (20, 10):
            share = self._share(self._batch(n))
            per, extra = divmod(n, 10)
            assert all(v == per for k, v in share.items() if k != 'wild'), (n, share)
            assert share['wild'] == per + extra, (n, share)

    def test_shares_ignore_the_instrument_offset(self):
        base = self._share(self._batch())
        for off in range(1, 8):
            assert self._share(self._batch(pool_offset=off)) == base

    def test_exploit_never_takes_a_wild_slot_and_still_fires(self):
        """EXPLOIT has no category file, so it is not one of the ten buckets — it
        converts the first standard slot, cadenced off the PERSISTENT creative
        walk. Cadenced off `i` instead it fired ZERO times: i=15 and i=30, while
        the deal's standard slots are i=6, 16 and 26."""
        import steering
        pool = list(ar.AutoResearcher.DEFAULT_INSTRUMENT_POOL)
        fired = 0
        for n in range(0, 45, 3):
            sch = ar._build_batch_schedule(
                pool, 31, 0, exploit_pool=POOL, academic_offset=0,
                creative_offset=n, macro_offset=0, pair_offset=0,
                steer=steering.Steering())
            ex = _exploit(sch)
            assert len(ex) <= 1, f'{len(ex)} exploit slots in one batch'
            assert all(not s[2] for s in ex)     # never the exploration floor
            fired += len(ex)
        assert fired >= 1, 'exploit unreachable across 15 batches'
