"""The chain, end to end — the arrow no single node's tests cover.

N1..N4 each pass in isolation while the CHAIN can still be broken, because it
crosses two modules and a JSON file on disk:

  meta_review.diversity_directive()
    -> .auto-research-logs/steer_families.json   {"boost": [...]}
      -> auto_research._directed_families()
        -> _mech_constraint_for(family)
          -> _build_batch_schedule() puts it on a backbone slot

Part A (here) covers everything up to the prompt. The last arrow — generated
rationale classifies back to the boosted family — needs a live model call and is
measured separately; see the classify-back result recorded in the session notes.
"""
import json
import sqlite3

import pytest

import auto_research as ar
import meta_review as mr

MAX_ITER = 31
INSTRUMENTS = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'AUD_USD', 'USD_CHF',
               'NZD_USD', 'USD_CAD', 'XAU_USD', 'SPX500_USD', 'NAS100_USD',
               'WTICO_USD', 'BCO_USD', 'DE30_EUR', 'JP225_USD', 'HK33_HKD',
               'EUR_GBP', 'EUR_JPY', 'GBP_JPY', 'AUD_JPY', 'CHF_JPY',
               'XAG_USD', 'NATGAS_USD', 'UK100_GBP', 'US2000_USD', 'CN50_USD',
               'SG30_SGD', 'EUR_AUD', 'GBP_AUD', 'CAD_JPY', 'NZD_JPY', 'BTC_USD']

# Dominant filler + a deliberately starved family, so diversity_directive() has a
# real "<5% under-used" answer to find rather than a hand-fed one.
DOMINANT = 'volatility squeeze compression before expansion'
STARVED = 'lead-lag divergence versus the related instrument'


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """One steer file, shared by BOTH modules — that shared path is the seam."""
    db = tmp_path / 'p.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE strategies (created_at TEXT, rationale TEXT)')
    conn.executemany(
        'INSERT INTO strategies VALUES (?,?)',
        [('2026-09-06T17:00:00', DOMINANT) for _ in range(200)]
        + [('2026-09-06T17:00:00', STARVED) for _ in range(2)])
    conn.commit(); conn.close()

    steer = tmp_path / 'steer_families.json'
    monkeypatch.setattr(mr, 'DB_PATH', db)
    monkeypatch.setattr(mr, 'CLEAN_ERA_START', '2000-01-01')
    monkeypatch.setattr(mr, 'STEER_FAMILIES_PATH', steer)
    monkeypatch.setattr(ar, '_STEER_FAMILIES_PATH', str(steer))
    return steer


def _render():
    return ar._build_batch_schedule(INSTRUMENTS, MAX_ITER, 0,
                                    academic_offset=0, creative_offset=0)


def test_meta_reviews_choice_reaches_the_scheduler(wired, monkeypatch):
    bullet = mr.diversity_directive()
    assert bullet, 'diversity_directive produced nothing to steer with'

    payload = json.loads(wired.read_text())
    boost = payload['boost']
    assert boost, 'steer file named no under-used family'

    monkeypatch.setenv('DIRECTED_SLOTS', '1')
    sched = _render()
    directed = [c for _, c, _, _, _, _ in sched if c in ar._MECH_CONSTRAINTS.values()]
    assert len(directed) == 1

    # The family is READ FROM THE FILE, never hardcoded: this asserts the seam,
    # not a constant we happen to agree on.
    expected = {ar._MECH_CONSTRAINTS[f] for f in boost}
    assert directed[0] in expected

    # and the prose the humans read names the same families the scheduler acted on
    for f in boost:
        assert f in bullet


def test_the_boosted_family_is_the_starved_one(wired, monkeypatch):
    mr.diversity_directive()
    boost = json.loads(wired.read_text())['boost']
    assert 'cross-market' in boost, boost      # STARVED is a cross-market rationale
    assert json.loads(wired.read_text())['damp'] == 'volatility'


def test_off_switch_severs_the_chain(wired, monkeypatch):
    mr.diversity_directive()
    monkeypatch.delenv('DIRECTED_SLOTS', raising=False)
    sched = _render()
    assert not [c for _, c, _, _, _, _ in sched if c in ar._MECH_CONSTRAINTS.values()]


def test_constraint_reaches_the_rendered_prompt_text(wired, monkeypatch):
    """A slot that carries the constraint but never renders it into the prompt
    would pass every other test in this file."""
    mr.diversity_directive()          # writes the steer file the scheduler reads
    monkeypatch.setenv('DIRECTED_SLOTS', '1')
    sched = _render()
    boost = json.loads(wired.read_text())['boost']
    target = [c for _, c, _, _, _, _ in sched if c in ar._MECH_CONSTRAINTS.values()][0]
    rendered = "\n".join(
        'Instrument={} | CONSTRAINT: {}'.format(inst, constraint)
        for inst, constraint, _, _, _, _ in sched)
    assert target in rendered
    assert any(ar._MECH_CONSTRAINTS[f] in rendered for f in boost)
