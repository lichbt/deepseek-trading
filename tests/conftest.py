"""Shared test isolation.

The provider circuit breaker (`auto_research._PROVIDER_HEALTH`) is deliberately
PROCESS-GLOBAL and survives for a 300s cooldown — correct in production, where a
batch should pay for an outage once rather than on every call. In a test session
it means one test that trips a provider silently reorders every later chain.

That is not hypothetical: it made three tests in test_self_critique.py fail in a
full-suite run while passing in isolation. `self_critique_thesis` correctly called
the demoted chain's new head (`ninerouter:thesis`) while the assertion compared
against the static alias `SELF_CRITIQUE_MODEL` (`byteplus:deepseek-v4-flash`), so
the failure looked like a routing bug and was really leaked state.

Reset it around every test so breaker state is never inherited.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_provider_breaker():
    try:
        import auto_research as ar
    except Exception:            # a test env without the module — nothing to reset
        yield
        return
    saved = dict(getattr(ar, '_PROVIDER_HEALTH', {}))
    ar._PROVIDER_HEALTH.clear()
    yield
    ar._PROVIDER_HEALTH.clear()
    ar._PROVIDER_HEALTH.update(saved)


# ── stale-bytecode guard ─────────────────────────────────────────────────────
# `sys.pycache_prefix` on this machine points OUTSIDE the repo
# (~/Library/Caches/com.apple.python/<abs path>), so clearing ./__pycache__ does
# nothing, and CPython invalidates a .pyc on (mtime, size) only. Flip a
# one-character constant back and forth inside a single second — exactly what
# mutation-testing a threshold does — and neither changes, so the interpreter
# happily keeps running bytecode compiled from the edited file.
#
# That is not hypothetical. On 2026-08-22 `validator.DSR_GATE_ENABLED` read True
# with DSR_GATE unset and the source plainly reading '0'; the executed line was a
# stale `environ.get('DSR_GATE', '1')`. It was nearly reported as a production
# finding — the DSR gate silently defaulting ON contradicts a binding decision.
#
# A prose warning is what the validation gates already had, and they stayed wrong
# for eight months. So this checks instead: once per session, compare each repo
# module's cached bytecode against a fresh compile of its source.
import importlib.util
import marshal
import py_compile
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _stale_pyc_modules():
    """Repo modules whose cached bytecode disagrees with their source."""
    stale = []
    for src in sorted(_REPO.glob('*.py')):
        cache = importlib.util.cache_from_source(str(src))
        if not cache or not Path(cache).exists():
            continue                     # never imported, or bytecode disabled
        try:
            # Recompile to a scratch path and compare the CODE, not the header:
            # the header carries the very (mtime, size) pair that failed us.
            # Compile from the ABSOLUTE path: co_filename is marshalled into the
            # code object, so compiling via a relative path makes every module
            # compare unequal for a reason that has nothing to do with staleness.
            fresh = Path(cache).with_suffix('.freshcheck')
            py_compile.compile(str(src.resolve()), cfile=str(fresh), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            # Compare the CODE, never the 16-byte header — the header carries the
            # very (mtime, size) pair that fails to notice this in the first place.
            cur = marshal.loads(Path(cache).read_bytes()[16:])
            new = marshal.loads(fresh.read_bytes()[16:])
            fresh.unlink(missing_ok=True)
            if (cur.co_code, cur.co_consts) != (new.co_code, new.co_consts):
                stale.append(src.name)
        except Exception:
            continue                     # a syntax error is another test's problem
    return stale


def pytest_sessionstart(session):
    stale = _stale_pyc_modules()
    if not stale:
        return
    where = sys.pycache_prefix or '<alongside each source file>'
    raise pytest.UsageError(
        'STALE BYTECODE — these modules would run code that is NOT what their '
        f'source says: {", ".join(stale)}\n'
        f'Cached bytecode lives at: {where}\n'
        'CPython invalidates on (mtime, size), so a same-size edit inside one '
        'second is invisible to it. Delete the cache for this repo and re-run; '
        'do NOT trust any measurement taken before you do.')


@pytest.fixture(autouse=True)
def _isolate_rotation_counters(tmp_path_factory):
    """Keep the persistent generation walks out of the repo during tests.

    `_build_batch_schedule` resumes AND WRITES BACK two counters when their
    offset argument is None: `.academic_rotation` (the anomaly walk) and
    `.creative_rotation` (the creative-constraint walk, added 2026-08-27). 31
    call sites across the suite pass no offset, so a plain `pytest tests/` was
    advancing production's real walks — and some of those calls render 18,000-slot
    schedules, which drove the creative counter to five digits in one run. A lost
    or jumped counter costs coverage rather than correctness, but a test run must
    not silently reach into the next batch's schedule.

    Redirect both to a temp dir for the whole session.
    """
    try:
        import auto_research as ar
    except Exception:
        yield
        return
    d = tmp_path_factory.mktemp('rotation')
    saved = (getattr(ar, '_ACADEMIC_ROTATION_FILE', None),
             getattr(ar, '_CREATIVE_ROTATION_FILE', None))
    ar._ACADEMIC_ROTATION_FILE = d / '.academic_rotation'
    ar._CREATIVE_ROTATION_FILE = d / '.creative_rotation'
    yield
    ar._ACADEMIC_ROTATION_FILE, ar._CREATIVE_ROTATION_FILE = saved
