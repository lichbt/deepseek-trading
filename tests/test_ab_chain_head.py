"""Tests for the generic chain-head A/B controller (AB_TEST_CHAIN).

Covers the four properties the design depends on (.scratch/thesis-ab/map.md):

  1. arms alternate, and twin batches SHARE a seed (so both arms see one schedule)
  2. every tagged candidate carries an explicit strategy_id — the arm is never
     recoverable only by created_at inference, which is the fingerprint A/B's defect
  3. a controller failure falls back to the CONTROL arm rather than mis-arming silently
  4. the swap actually moves the head of the named chain, for any chain
"""
import json

import pytest

import auto_research as ar


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the controller at a temp dir and reset its module state per test.

    _ab_apply_arm mutates module globals by design (one batch per process in the real
    loop), so every global it can touch is snapshotted and restored here — otherwise the
    swap leaks into unrelated tests that assert on the configured chain.
    """
    monkeypatch.setattr(ar, '_AB_DIR', tmp_path / '.ab_test')
    monkeypatch.setattr(ar, '_AB_PAIR_SEED', None)
    saved = {name: getattr(ar, name)
             for name in list(ar._AB_CHAIN_VARS.values()) + ['THESIS_MODEL', 'DEFAULT_MODEL']}
    ar._AB_STATE.clear()
    for var in ('AB_TEST_CHAIN', 'AB_ARM_CONTROL', 'AB_ARM_CHALLENGER',
                'AB_TEST_FINGERPRINT'):
        monkeypatch.delenv(var, raising=False)
    yield
    for name, value in saved.items():
        setattr(ar, name, value)
    ar._AB_STATE.clear()


def _arm_env(monkeypatch, chain='thesis'):
    monkeypatch.setenv('AB_TEST_CHAIN', chain)
    monkeypatch.setenv('AB_ARM_CONTROL', 'byteplus:ctrl-model')
    monkeypatch.setenv('AB_ARM_CHALLENGER', 'byteplus:chal-model')


# ── 1. alternation and pairing ────────────────────────────────────────────────

def test_arms_alternate_and_twins_share_a_seed(monkeypatch):
    _arm_env(monkeypatch)
    arms = [ar._ab_chain_arm() for _ in range(6)]

    assert [a['arm'] for a in arms] == [
        'control', 'challenger', 'control', 'challenger', 'control', 'challenger']
    # (0,1) -> 0, (2,3) -> 1, (4,5) -> 2: each twin pair shares one seed
    assert [a['seed'] for a in arms] == [0, 0, 1, 1, 2, 2]


def test_pinned_seed_drives_the_schedule_rotation(monkeypatch):
    """Both halves of a twin must resolve the SAME asset-mode constraint."""
    _arm_env(monkeypatch)
    inst = next(iter(ar._ASSET_MODE_CONCEPTS))

    a = ar._ab_chain_arm()                      # batch 0, seed 0
    first = ar._asset_mode_for(inst)
    b = ar._ab_chain_arm()                      # batch 1, seed 0 — the twin
    second = ar._asset_mode_for(inst)

    assert a['seed'] == b['seed']
    assert first == second, 'twin batches must see an identical schedule'


def test_next_pair_rotates_off_the_previous_seed(monkeypatch):
    """Pairing must not freeze the schedule — only twins share, pairs differ."""
    _arm_env(monkeypatch)
    inst = next(iter(ar._ASSET_MODE_CONCEPTS))
    seen = []
    for _ in range(len(ar._ASSET_MODE_CONCEPTS[inst]) * 2):
        ar._ab_chain_arm()
        seen.append(ar._asset_mode_for(inst))
    assert len(set(seen)) > 1, 'the pinned seed must still advance across pairs'


# ── 2. explicit strategy_id tagging ───────────────────────────────────────────

def test_tag_records_explicit_strategy_id_and_provenance(monkeypatch):
    _arm_env(monkeypatch)
    ar._ab_apply_arm(ar._ab_chain_arm())
    ar._ab_tag_candidate('eurusd_auto_20260810_000000_i1',
                         instrument='EUR_USD', timeframe='D')

    rows = [json.loads(l) for l in
            (ar._AB_DIR / 'tags-thesis.jsonl').read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row['strategy_id'] == 'eurusd_auto_20260810_000000_i1'
    assert row['arm'] == 'control'
    assert row['model'] == 'byteplus:ctrl-model'
    assert row['instrument'] == 'EUR_USD'
    # provenance keys must exist so the analysis can detect a mid-run code change
    assert 'git_sha' in row and 'git_branch' in row


def test_tag_is_a_noop_when_the_ab_is_inactive():
    ar._ab_tag_candidate('some_sid')
    assert not (ar._AB_DIR / 'tags-thesis.jsonl').exists()


def test_tag_never_raises_when_the_sidecar_is_unwritable(monkeypatch, capsys):
    _arm_env(monkeypatch)
    ar._ab_apply_arm(ar._ab_chain_arm())

    def _boom(*a, **k):
        raise OSError('disk full')

    monkeypatch.setattr('builtins.open', _boom)
    ar._ab_tag_candidate('sid_x')           # must not propagate into the research loop
    assert 'tag write failed' in capsys.readouterr().out


# ── 3. fail closed ────────────────────────────────────────────────────────────

def test_counter_failure_falls_back_to_control(monkeypatch, capsys):
    _arm_env(monkeypatch)

    class _Boom:
        def mkdir(self, **k):
            raise OSError('read-only fs')

        def __truediv__(self, other):
            raise OSError('read-only fs')

    monkeypatch.setattr(ar, '_AB_DIR', _Boom())
    arm = ar._ab_chain_arm()

    assert arm['arm'] == 'control', 'a broken counter must never mis-arm a batch'
    assert arm['failed_closed'] is True
    assert 'falling back to CONTROL' in capsys.readouterr().out


def test_missing_arm_models_refuses(monkeypatch, capsys):
    monkeypatch.setenv('AB_TEST_CHAIN', 'thesis')       # no arm models set
    assert ar._ab_chain_arm() is None
    assert 'REFUSING' in capsys.readouterr().out


def test_unknown_chain_is_ignored(monkeypatch, capsys):
    _arm_env(monkeypatch, chain='nonsense')
    assert ar._ab_chain_arm() is None
    assert 'not one of' in capsys.readouterr().out


def test_refuses_to_run_alongside_the_fingerprint_ab(monkeypatch, capsys):
    _arm_env(monkeypatch)
    monkeypatch.setenv('AB_TEST_FINGERPRINT', '1')
    assert ar._ab_chain_arm() is None
    assert 'confounds' in capsys.readouterr().out


def test_inactive_by_default():
    assert ar._ab_chain_arm() is None


# ── 4. the swap moves the real chain head ─────────────────────────────────────

@pytest.mark.parametrize('chain,var', sorted(ar._AB_CHAIN_VARS.items()))
def test_swap_moves_the_named_chain_head(monkeypatch, chain, var):
    _arm_env(monkeypatch, chain=chain)
    original = list(getattr(ar, var))
    monkeypatch.setattr(ar, var, original)

    ar._ab_apply_arm(ar._ab_chain_arm())            # batch 0 -> control
    assert getattr(ar, var)[0] == 'byteplus:ctrl-model'

    ar._ab_apply_arm(ar._ab_chain_arm())            # batch 1 -> challenger
    assert getattr(ar, var)[0] == 'byteplus:chal-model'
    # the swap must not duplicate the arm further down the chain
    assert getattr(ar, var).count('byteplus:chal-model') == 1


def test_thesis_swap_keeps_derived_aliases_consistent(monkeypatch):
    _arm_env(monkeypatch)
    monkeypatch.setattr(ar, 'THESIS_MODELS', list(ar.THESIS_MODELS))
    ar._ab_chain_arm()                              # batch 0
    ar._ab_apply_arm(ar._ab_chain_arm())            # batch 1 -> challenger

    assert ar.THESIS_MODEL == 'byteplus:chal-model'
    assert ar.DEFAULT_MODEL == 'byteplus:chal-model'


def test_swap_preserves_the_fallback_chain(monkeypatch):
    """Swapping the head must not drop the rest of the chain — outage cover matters."""
    _arm_env(monkeypatch)
    monkeypatch.setattr(ar, 'THESIS_MODELS', ['a', 'byteplus:chal-model', 'b'])
    ar._ab_chain_arm()
    ar._ab_apply_arm(ar._ab_chain_arm())            # challenger

    assert ar.THESIS_MODELS == ['byteplus:chal-model', 'a', 'b']
