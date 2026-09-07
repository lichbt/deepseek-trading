"""DIRECTED slot: the directive's only structural lever.

The checkpoint this file exists to enforce (N3): with the slot on, EVERY forced
family's slot count at the production batch length must be byte-identical to the
count with it off. The scheduler's own comments record three families whose
residues silently fired zero times in production; a lever that quietly eats
academic or wild would be the same defect wearing different numbers.
"""
import json
import pytest
import auto_research as ar

MAX_ITER = 31          # run_forever.sh production value
INSTRUMENTS = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'AUD_USD', 'USD_CHF',
               'NZD_USD', 'USD_CAD', 'XAU_USD', 'SPX500_USD', 'NAS100_USD',
               'WTICO_USD', 'BCO_USD', 'DE30_EUR', 'JP225_USD', 'HK33_HKD',
               'EUR_GBP', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY', 'CHF_JPY',
               'XAG_USD', 'NATGAS_USD', 'UK100_GBP', 'US2000_USD', 'CN50_USD',
               'SG30_SGD', 'EUR_AUD', 'GBP_AUD', 'CAD_JPY', 'NZD_JPY', 'BTC_USD']


def _render(**kw):
    return ar._build_batch_schedule(INSTRUMENTS, MAX_ITER, kw.pop('pool_offset', 0),
                                    academic_offset=0, creative_offset=0, **kw)


def _families(sched):
    """Classify each slot by the constraint it carries, using the same texts the
    scheduler assigns — so this counts SLOTS, not prose."""
    out = []
    for inst, constraint, wild, i, detector, tf in sched:
        if wild:
            out.append('wild')
        elif constraint in ar._MECH_CONSTRAINTS.values():
            out.append('directed')
        elif constraint in ar._CREATIVE_CONSTRAINTS:
            out.append('creative')
        else:
            out.append('forced')
    return out


@pytest.fixture
def steer(tmp_path, monkeypatch):
    p = tmp_path / 'steer_families.json'
    p.write_text(json.dumps({'boost': ['cross-market', 'event'],
                             'damp': 'volatility', 'n': 63415, 'ts': 'x'}))
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(p))
    return p


def test_off_by_default(monkeypatch, steer):
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    assert 'directed' not in _families(_render())


def test_fires_exactly_n_times_at_production_batch_length(monkeypatch, steer):
    """The asset slot's defect: a residue that renders fine and fires zero times."""
    for n in (1, 2, 3):
        monkeypatch.setenv('DIRECTED_SLOTS', str(n))
        assert _families(_render()).count('directed') == n, f'DIRECTED_SLOTS={n}'


def test_no_forced_family_loses_a_slot(monkeypatch, steer):
    """THE checkpoint. Directed slots come out of the creative backbone only."""
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    before = _families(_render())
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    after = _families(_render())

    assert before.count('forced') == after.count('forced')
    assert before.count('wild') == after.count('wild')
    assert after.count('directed') == 2
    assert after.count('creative') == before.count('creative') - 2
    # and every non-creative slot is untouched, position by position
    base = _render.__wrapped__ if hasattr(_render, '__wrapped__') else None
    for k, (b, a) in enumerate(zip(before, after)):
        if b in ('forced', 'wild'):
            assert a == b, f'slot {k} changed from {b} to {a}'


def test_directed_slots_are_the_creative_ones(monkeypatch, steer):
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    before = _families(_render())
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    after = _families(_render())
    for k, (b, a) in enumerate(zip(before, after)):
        if a == 'directed':
            assert b == 'creative', f'slot {k} was {b}, not a backbone slot'


def test_boosted_family_is_what_the_steer_named(monkeypatch, steer):
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    sched = _render()
    got = {c for _, c, _, _, _, _ in sched if c in ar._MECH_CONSTRAINTS.values()}
    allowed = {ar._MECH_CONSTRAINTS['cross-market'], ar._MECH_CONSTRAINTS['event']}
    assert got and got <= allowed


def test_day_resolution_family_is_pinned_to_daily(monkeypatch, tmp_path):
    """A directed 'event' slot must not land on weekly bars — ~48% of weeks
    contain an event, which is the zero-selectivity failure the tf block records."""
    p = tmp_path / 's.json'
    p.write_text(json.dumps({'boost': ['event'], 'damp': 'volatility', 'n': 99}))
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(p))
    monkeypatch.setenv('DIRECTED_SLOTS', '3')
    for inst, c, wild, i, detector, tf in _render():
        if c == ar._MECH_CONSTRAINTS['event']:
            assert tf == 'D', f'event slot i={i} got tf={tf}'
            assert detector is None, f'event slot i={i} kept detector {detector}'


def test_non_day_family_keeps_its_rotated_timeframe(monkeypatch, tmp_path):
    p = tmp_path / 's.json'
    p.write_text(json.dumps({'boost': ['cross-market'], 'damp': 'x', 'n': 99}))
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(p))
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    before = {i: tf for _, _, _, i, _, tf in _render()}
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    for inst, c, wild, i, detector, tf in _render():
        if c == ar._MECH_CONSTRAINTS['cross-market']:
            assert tf == before[i]   # unchanged by the swap


@pytest.mark.parametrize('payload', ['', 'not json', '{}', '{"boost": []}',
                                     '{"boost": ["no-such-family"]}',
                                     '{"boost": [1, 2]}'])
def test_bad_or_missing_steer_file_is_a_no_op(monkeypatch, tmp_path, payload):
    p = tmp_path / 's.json'
    p.write_text(payload)
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(p))
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    baseline = _families(_render())
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    assert _families(_render()) == baseline


def test_absent_steer_file_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(tmp_path / 'nope.json'))
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    baseline = _families(_render())
    monkeypatch.setenv('DIRECTED_SLOTS', '2')
    assert _families(_render()) == baseline


def test_cannot_claim_more_than_the_backbone_holds(monkeypatch, steer):
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    backbone = _families(_render()).count('creative')
    monkeypatch.setenv('DIRECTED_SLOTS', '999')
    after = _families(_render())
    assert after.count('directed') == backbone
    assert after.count('creative') == 0
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    assert _families(_render()).count('forced') == after.count('forced')


def test_garbage_env_value_is_off(monkeypatch, steer):
    monkeypatch.setenv('DIRECTED_SLOTS', 'yes-please')
    assert ar._directed_slots() == 0
    assert 'directed' not in _families(_render())


def test_picks_are_the_tail_of_the_backbone(monkeypatch, steer):
    """Not cosmetic: the creative constraint index and the timeframe advance
    together, so the constraints a batch renders must stay a contiguous run from
    the walk's base. Taking from the tail is what keeps that true."""
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    before = _families(_render())
    backbone = [k for k, f in enumerate(before) if f == 'creative']
    monkeypatch.setenv('DIRECTED_SLOTS', '3')
    after = _families(_render())
    idx = [k for k, f in enumerate(after) if f == 'directed']
    assert len(idx) == len(set(idx)) == 3
    assert idx == backbone[-3:]
    # the surviving creative slots are the HEAD of the backbone, unbroken
    assert [k for k, f in enumerate(after) if f == 'creative'] == backbone[:-3]
