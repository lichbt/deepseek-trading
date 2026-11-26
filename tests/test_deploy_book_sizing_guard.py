"""deploy_book.sh must notice when a rollout resized the whole book.

BASE_RISK and BOOK_SCALE are set with `kubectl set env` on the k3s deployment —
not in the Zeabur dashboard, not in `.env` — so a rollout reconciles the spec back
to the dashboard value and every position re-sizes (0.0145 -> 0.007 halves the
book; BOOK_SCALE -> 1.0 adds ~5%). Nothing caught that until 2026-10-12, when
BOOK_SCALE=0.9511 was hand-set to hold magnitude after the hk33hkd_i26 retire and
the only evidence it was live was a line buried in the pod log.

The guard is deploy-path shell, so it cannot be executed without a deploy. These
pin the pieces that can rot silently: that the snapshot/compare is still there,
that drift fails the run instead of printing a line nobody reads, and that the
pod-log evidence line is parsed by the same regex the human reads.
"""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / 'scripts' / 'deploy_book.sh'
SRC = SCRIPT.read_text()

# Verbatim from the pod's startup log, 2026-10-12.
POD_LINE = ('[2026-10-12T03:09:39] RISK/trade=1.45% x BOOK_SCALE 0.9511 = '
            'EFFECTIVE 1.379%  MAXRISK=2%  (set BASE_RISK / BOOK_SCALE in .env)')
EFF_SED = r's/.*\(RISK\/trade=[0-9.]*% x BOOK_SCALE [0-9.]* = EFFECTIVE [0-9.]*%\).*/\1/p'


def test_snapshots_both_knobs_before_the_push():
    pattern = "grep -E '^(BASE_RISK|BOOK_SCALE)='"
    assert SRC.count(pattern) == 2, 'knob snapshot must be read both sides of the rollout'
    before = SRC.index('KNOBS_BEFORE=')
    after = SRC.index('KNOBS_AFTER=')
    assert before < after, 'KNOBS_BEFORE must be read before the rollout'


def test_drift_fails_the_run_not_just_warns():
    assert 'KNOBS_BEFORE" != "$KNOBS_AFTER' in SRC, 'drift comparison removed'
    assert 'FAIL: pod sizing knobs CHANGED' in SRC
    assert 'exit 1' in SRC[SRC.index('FAIL: sizing drift'):]
    assert 'sizing_ok=$SIZING_OK' in SRC, 'deploy log no longer records the verdict'


def test_pod_log_evidence_line_is_parsed():
    out = subprocess.run(['sed', '-n', EFF_SED], input=POD_LINE,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == 'RISK/trade=1.45% x BOOK_SCALE 0.9511 = EFFECTIVE 1.379%'
    # The script's own copy of the pattern must be the one just tested.
    assert EFF_SED.replace('\\', '\\\\') in SRC or EFF_SED in SRC, \
        'script regex and test regex have drifted apart'


def _arm(script: str, verb: str) -> str:
    m = re.search(r'\n\s*' + verb + r'\)(.*?)\n\s*;;', script, re.S)
    assert m, f'no `{verb})` arm in the interlock script'
    return m.group(1)


def test_interlock_off_resumes_trading():
    """`off` must scale back to 1 replica.

    The quota only ever GATED pods, and an env-only change has no push to restore
    `spec.replicas` — that mismatch left the prop book at 0 pods for ~40 minutes on
    2026-10-12 with nothing reporting it. `off` releasing the interlock has to mean
    the book trades again.
    """
    z = (Path(__file__).parent.parent / 'scripts' / 'zeabur_interlock.sh').read_text()
    off = _arm(z, 'off')
    assert 'delete quota' in off
    assert '--replicas=1' in off, 'off no longer scales the deployment back up'
    assert off.index('delete quota') < off.index('--replicas=1'), \
        'the scale must come after the quota is gone, or the new pod hits pods=0'
    assert '--replicas=0' in _arm(z, 'on')


def test_deploy_doc_records_that_off_resumes():
    deploy_doc = (Path(__file__).parent.parent / '.claude' / 'skills' / 'sleeve-ops'
                  / 'references' / 'deploy.md').read_text()
    flat = ' '.join(deploy_doc.split())   # doc wraps mid-sentence; assert on the text
    assert 'resumes trading' in flat
    assert 'scales the deployment back to 1 replica' in flat