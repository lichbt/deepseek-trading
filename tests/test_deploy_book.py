"""Guards for scripts/deploy_book.sh - the one-command book publish.

Two invariants that cost real money when broken: the script must never
git-add the 485 MB working pipeline.db (2026-08-05 put it in a commit), and
--dry-run must change nothing at all.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy_book.sh"
PODWT = ROOT / ".podwt"


def _head():
    return subprocess.run(["git", "-C", str(PODWT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout


def test_dry_run_changes_nothing():
    if not PODWT.exists():
        pytest.skip("no .podwt worktree on this machine")
    state = ROOT / "portfolio_state.json"
    head_before = _head()
    before = state.stat().st_mtime_ns
    r = subprocess.run([str(SCRIPT), "--dry-run", "--reason", "pytest"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "DRY" in r.stdout
    assert _head() == head_before
    assert state.stat().st_mtime_ns == before


def test_never_git_adds_the_working_db():
    src = SCRIPT.read_text()
    assert "update-index --cacheinfo" in src
    bad = [l for l in src.splitlines()
           if "add pipeline.db" in l and not l.strip().startswith("#")]
    assert bad == [], bad
