"""The token blob must survive a pod restart.

The refresh token ROTATES. Persisting it beside the module means /app in the
container, which is ephemeral — so the pod's own refresh wrote the replacement
somewhere that dies on restart, and the next boot fell back to the STATIC
CTRADER_TOKENS env blob whose refresh token that same refresh had invalidated.
Any restart after any refresh came up dead. Observed 2026-09-07: the pod ran
2d1h, was restarted, and returned CH_ACCESS_TOKEN_INVALID / NOT TRADING.

No second host is required for that failure, which is why these tests exist.
"""
import os

import ctrader_client as cc


def test_local_run_is_unchanged(tmp_path):
    """No symlink means a dev machine: keep writing beside the module."""
    assert cc._resolve_token_dir(here=str(tmp_path)) == str(tmp_path)


def test_the_pod_resolves_onto_the_volume(tmp_path):
    """/app/fix_runner_state.json is a symlink to /data — realpath finds it,
    with no new env var to forget (the reason prop_guard does it this way)."""
    app = tmp_path / 'app'; app.mkdir()
    volume = tmp_path / 'data'; volume.mkdir()
    (volume / 'fix_runner_state.json').write_text('{}')
    os.symlink(str(volume / 'fix_runner_state.json'),
               str(app / 'fix_runner_state.json'))
    assert cc._resolve_token_dir(here=str(app)) == str(volume)


def test_a_dangling_symlink_falls_back_rather_than_writing_nowhere(tmp_path):
    app = tmp_path / 'app'; app.mkdir()
    os.symlink(str(tmp_path / 'gone' / 'fix_runner_state.json'),
               str(app / 'fix_runner_state.json'))
    assert cc._resolve_token_dir(here=str(app)) == str(app)


def test_a_plain_file_is_not_mistaken_for_the_volume(tmp_path):
    """A real fix_runner_state.json (not a symlink) is a local run, not the pod."""
    app = tmp_path / 'app'; app.mkdir()
    (app / 'fix_runner_state.json').write_text('{}')
    assert cc._resolve_token_dir(here=str(app)) == str(app)


def test_an_explicit_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('CTRADER_TOKEN_DIR', str(tmp_path))
    assert cc._resolve_token_dir(here='/somewhere/else') == str(tmp_path)


def test_the_token_file_sits_in_the_resolved_dir():
    assert cc._TOKEN_FILE == os.path.join(cc._TOKEN_DIR, '.ctrader_tokens.json')
    assert cc._TOKEN_FILE.endswith('.ctrader_tokens.json')


def test_a_rotated_pair_round_trips_through_the_resolved_path(tmp_path, monkeypatch):
    """The whole point: what _save_tokens writes is what the NEXT boot loads —
    not the env seed the refresh already invalidated."""
    monkeypatch.setattr(cc, '_TOKEN_FILE', str(tmp_path / '.ctrader_tokens.json'))
    monkeypatch.delenv('CTRADER_TOKENS', raising=False)
    rotated = {'access_token': 'new-access', 'refresh_token': 'ROTATED',
               'expires_at': 1791169581.0, 'created_at': 1788577581.0}
    cc._save_tokens(rotated)
    assert cc._load_tokens()['refresh_token'] == 'ROTATED'


def test_the_env_seed_is_still_honoured_when_set(tmp_path, monkeypatch):
    """CTRADER_TOKENS remains the seed for a volume that has no file yet."""
    import json
    monkeypatch.setattr(cc, '_TOKEN_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setenv('CTRADER_TOKENS', json.dumps(
        {'access_token': 'seed', 'refresh_token': 'SEED'}))
    assert cc._load_tokens()['refresh_token'] == 'SEED'


def test_a_failed_persist_warns_about_the_next_restart(tmp_path, monkeypatch, capsys):
    """A silent failure here is the whole bug: the session keeps working and the
    next boot is dead. It must say so."""
    monkeypatch.setattr(cc, '_TOKEN_FILE', str(tmp_path / 'nodir' / 'x.json'))
    cc._save_tokens({'access_token': 'a', 'refresh_token': 'b'})
    out = capsys.readouterr().out
    assert 'WARNING' in out
    assert 'next restart' in out.lower() or 'NEXT restart' in out
