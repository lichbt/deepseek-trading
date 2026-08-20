"""
Auto Research: Automated strategy generation + validation loop.
Uses OpenRouter (Gemini Flash / Claude) to generate candidates, then runs them
through the validator, records results, and iterates.

Usage:
    python auto_research.py --target 3 --max-iter 20 --instrument EUR_USD

Or programmatically:
    from auto_research import AutoResearcher
    ar = AutoResearcher(instruments=['EUR_USD'])
    ar.run(target_passed=3, max_iterations=20)
"""

import os
import re
import sys
import json
import time
import random
import hashlib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

import pipeline_utils as pu
from validator import validate_strategy, create_strategy_function
from telegram_bot import notify_strategy_passed, notify_research_complete


# ============================================================================
# CONFIGURATION
# ============================================================================


def _load_dotenv() -> None:
    """Delegates to env_loader — this was a third private copy of the same parser
    (see portfolio._load_dotenv). Same semantics: existing env vars win."""
    from env_loader import load_env
    load_env(Path(__file__).parent / '.env')


_load_dotenv()

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
OPENROUTER_BASE = 'https://openrouter.ai/api/v1'

# BytePlus (ModelArk coding plan) — the PAID tier. A model id prefixed 'byteplus:'
# routes to the BytePlus OpenAI-compatible endpoint; everything else stays on
# OpenRouter (the free tier). Keeps one code path, per-model routing.
BYTEPLUS_BASE = os.getenv('BYTEPLUS_BASE_URL', '')
BYTEPLUS_KEY = os.getenv('BYTEPLUS_API_TOKEN', '')
_BYTEPLUS_PREFIX = 'byteplus:'
CLINE_BASE = os.getenv('CLINE_BASE_URL', '')
CLINE_KEY = os.getenv('CLINE_API_TOKEN', '')
_CLINE_PREFIX = 'cline:'
OPENCODE_BASE = os.getenv('OPENCODE_BASE_URL', '')
OPENCODE_KEY = os.getenv('OPENCODE_API_TOKEN', '')
_OPENCODE_PREFIX = 'opencode:'
NINEROUTER_BASE = os.getenv('NINEROUTER_ENDPOINT', '')
NINEROUTER_KEY = os.getenv('NINEROUTER_API_KEY', '')
_NINEROUTER_PREFIX = 'ninerouter:'
# Alibaba Cloud MaaS (compatible-mode/v1) — reasoning models; see the max_tokens
# note at THESIS_SINGLE_MAX_TOKENS, a tight cap is spent on reasoning and returns
# empty content.
ALIBABA_BASE = os.getenv('ALIBABA_BASE_URL', '')
ALIBABA_KEY = os.getenv('ALIBABA_API_TOKEN', '')
_ALIBABA_PREFIX = 'alibaba:'


def _route_model(model: str, api_key: str = None):
    """(base_url, api_key, clean_model, is_direct) for a model id."""
    if model and model.startswith(_BYTEPLUS_PREFIX):
        return BYTEPLUS_BASE, BYTEPLUS_KEY, model[len(_BYTEPLUS_PREFIX):], True
    if model and model.startswith(_CLINE_PREFIX):
        return CLINE_BASE, CLINE_KEY, model[len(_CLINE_PREFIX):], True
    if model and model.startswith(_OPENCODE_PREFIX):
        return OPENCODE_BASE, OPENCODE_KEY, model[len(_OPENCODE_PREFIX):], True
    if model and model.startswith(_NINEROUTER_PREFIX):
        return NINEROUTER_BASE, NINEROUTER_KEY, model[len(_NINEROUTER_PREFIX):], True
    if model and model.startswith(_ALIBABA_PREFIX):
        return ALIBABA_BASE, ALIBABA_KEY, model[len(_ALIBABA_PREFIX):], True
    return OPENROUTER_BASE, (api_key or OPENROUTER_API_KEY), model, False


# ── Provider circuit breaker ────────────────────────────────────────────────
# Every chain is mirrored across two providers (opencode + cline). Plain
# sequential failover is CORRECT during a provider outage but slow: each call
# re-walks the dead provider's entries first, paying a full timeout per entry.
# The breaker demotes a provider that keeps failing at the TRANSPORT level, so
# the healthy provider is tried first for a cooldown and the outage is paid for
# once instead of on every call.
_PROVIDER_TRIP_THRESHOLD = 3     # consecutive provider-level failures to trip
_PROVIDER_COOLDOWN = 300         # seconds a tripped provider stays demoted
_PROVIDER_HEALTH: Dict[str, Dict[str, float]] = {}   # prefix -> {fails, until}


def _provider_of(model: str) -> str:
    """The provider prefix of a chain entry ('opencode:', 'cline:', ...)."""
    for p in (_BYTEPLUS_PREFIX, _CLINE_PREFIX, _OPENCODE_PREFIX, _NINEROUTER_PREFIX,
              _ALIBABA_PREFIX):
        if model and model.startswith(p):
            return p
    return 'openrouter:'


def _is_provider_level_err(err: str) -> bool:
    """True only for failures that indict the PROVIDER, not the model.

    Empty content, JSON parse failures and model errors are MODEL-level: one bad
    generation must never sideline a provider that is answering fine. Only
    transport failures (timeout / connection / 5xx / 429) count toward a trip.
    """
    e = (err or '').lower()
    if ('empty content' in e or 'failed to parse' in e or 'model error' in e
            or 'prompt too large' in e or 'not set' in e):
        return False
    return ('timeout' in e or 'connection' in e or 'remotedisconnected' in e
            or 'api error' in e or 'request error' in e
            or ' 429' in e or 'http 5' in e)


def _record_provider_result(model: str, ok: bool, err: str = None) -> None:
    """Feed one call outcome into the breaker."""
    prov = _provider_of(model)
    st = _PROVIDER_HEALTH.setdefault(prov, {'fails': 0, 'until': 0.0})
    if ok:
        if st['fails'] or st['until']:
            print(f'  [Provider] {prov.rstrip(":")} healthy again', flush=True)
        st['fails'], st['until'] = 0, 0.0
        return
    if not _is_provider_level_err(err):
        return                                   # model-level — provider not blamed
    st['fails'] += 1
    if st['fails'] >= _PROVIDER_TRIP_THRESHOLD and time.time() >= st['until']:
        st['until'] = time.time() + _PROVIDER_COOLDOWN
        print(f'  [Provider] {prov.rstrip(":")} tripped after {int(st["fails"])} '
              f'transport failures — demoted for {_PROVIDER_COOLDOWN}s', flush=True)


def _chain_order(models):
    """Chain reordered so tripped providers go LAST (stable within each group).

    Never drops an entry. If every provider is tripped, or none is, the order is
    returned unchanged — so there is no 'all models skipped' failure mode and the
    cooldown expiring naturally re-promotes the provider for a half-open probe.
    """
    now = time.time()
    healthy, tripped = [], []
    for m in models:
        (tripped if _PROVIDER_HEALTH.get(_provider_of(m), {}).get('until', 0.0) > now
         else healthy).append(m)
    if not tripped or not healthy:
        return list(models)
    print(f'  [Provider] chain reordered — {healthy[0]} first, '
          f'{len(tripped)} demoted entr{"y" if len(tripped) == 1 else "ies"} last', flush=True)
    return healthy + tripped


# ── Reasoning effort cap ────────────────────────────────────────────────────
# The opencode/cline gateways proxy DeepSeek reasoning models that, left
# uncapped, burn 50k+ chain-of-thought tokens into reasoning_content BEFORE the
# first answer token — so an answer-sized max_tokens budget is spent entirely on
# reasoning and returns finish_reason=length with content=''. Measured
# 2026-07-23: deepseek-v4-pro at max_tokens=12000 hit length with 0 content every
# call. `reasoning:{effort:'low'}` (object form) is honored by the gateway and
# drops it to ~5k completion tokens with a clean finish=stop; `reasoning_effort:
# 'none'` 400s upstream and `{enabled:false}` is ignored — so 'low' is the lever.
# Applies only to gateway-proxied providers (opencode/cline); '' disables.
REASONING_EFFORT = os.getenv('REASONING_EFFORT', 'low').strip()
_REASONING_PROVIDERS = (_OPENCODE_PREFIX, _CLINE_PREFIX)

# Not every gateway-proxied model accepts the field. Measured 2026-07-24:
# opencode:glm-5.2 400s on 100% of requests carrying `reasoning`, with an opaque
# body ("Error from provider (Console Go): Upstream request failed") that never
# names the field. glm-5.2 leads the CODEGEN and CRITIQUE chains, so every call
# opened with a guaranteed 400 and fell through to slow models — batches went
# from 31 iterations in ~25 min to hitting the 2 h watchdog at ~20 iterations.
# Rejections are learned at runtime and remembered process-wide, so a model pays
# the wasted round-trip once per batch instead of once per call.
_REASONING_UNSUPPORTED = set()


def _mark_reasoning_unsupported(model: str):
    """Remember that `model` 400s when sent the `reasoning` field."""
    if model and model not in _REASONING_UNSUPPORTED:
        _REASONING_UNSUPPORTED.add(model)
        print(f'  [Reasoning] {model} rejects the reasoning field — '
              f'omitting it for the rest of this batch', flush=True)


# Per-model reasoning-effort overrides. REASONING_EFFORT is the global default
# for every gateway model; this lets a specific model diverge. Format (comma sep):
#   REASONING_EFFORT_OVERRIDES=opencode:deepseek-v4-flash=none,opencode:foo=low
# An empty value (…=) omits the field entirely for that model. Keys are the
# PREFIXED chain ids. Measured 2026-07-24: flash accepts effort:none cleanly (no
# 400 in 9 calls, 8/8 valid, marginally faster/leaner than low) — the pro-era
# note above (none 400s) does NOT apply to flash on the current gateway.
def _parse_reasoning_overrides(raw: str) -> dict:
    out = {}
    for pair in raw.split(','):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        mdl, _, eff = pair.partition('=')
        out[mdl.strip()] = eff.strip()
    return out


_REASONING_OVERRIDES = _parse_reasoning_overrides(os.getenv('REASONING_EFFORT_OVERRIDES', ''))


def _alibaba_thinking_off(model: str) -> dict:
    """Extra payload fields that switch an Alibaba MaaS model out of thinking mode.

    Alibaba's compatible-mode models emit `reasoning_content` BEFORE any answer
    token, and max_tokens is a budget over both — so deepseek-v4-pro-0813 spends
    the whole thesis (2500) and even the codegen (12000) budget reasoning and
    returns finish_reason=length with empty content. `reasoning` (the OpenRouter
    cap field) is not understood here; `enable_thinking: false` is. Set
    ALIBABA_THINKING=1 to keep thinking on (then raise max_tokens accordingly).
    """
    if not (model or '').startswith(_ALIBABA_PREFIX):
        return {}
    if os.getenv('ALIBABA_THINKING', '').strip() in ('1', 'true', 'yes'):
        return {}
    return {'enable_thinking': False}


def _reasoning_param(model: str):
    """The `reasoning` payload field for a chain entry, or None to omit it.

    Keyed on the ORIGINAL prefixed model id (before _route_model strips it), so
    call it with the chain entry, not the cleaned model name.
    """
    if (not model or not model.startswith(_REASONING_PROVIDERS)
            or model in _REASONING_UNSUPPORTED):
        return None
    effort = _REASONING_OVERRIDES.get(model, REASONING_EFFORT)
    if effort:
        return {'effort': effort}
    return None


# Thesis generation.
#
# EXPERIMENT (2026-05-26): swapped primary to deepseek-v4-flash:free to compare
# thesis quality / JSON reliability / fallback rate.
# REVERTED (2026-06-01): deepseek-v4-flash:free returned HTTP 429 on 53/53
# batches on 2026-05-31 — a 100% fallback rate, so every thesis was actually
# generated by gpt-oss-120b anyway (just after a wasted 429 + retry per batch).
# 2026-06-28: deepseek-v4-flash (PAID) leads THESIS for reliability — paid models route to
# paid providers with real capacity, so no free-tier upstream 429s (fully-free cascaded to
# ~26 errors/batch). v4-flash is the cheap paid model that ran thesis primary for weeks (~26k
# calls); thesis is one batched call/batch so cost is small. Free + deepseek-chat back it up;
# CODE is gpt-oss:free-led + paid deepseek-chat backstop below.
def _configured_models(*models):
    """Filter a raw model list: drop empty entries and tokens ending with ':'
    (an unresolved `cline:`/`ninerouter:` prefix with no model after it)."""
    return [m for m in models if m and not m.endswith(':')]


def _parse_model_chain(env_var, default):
    """Parse a comma-separated model chain from an env var into a clean list.

    Values look like: THESIS_MODELS=cline:foo,byteplus:bar,ninerouter:thesis
    Returns the default when the env var is unset or effectively empty. Trims
    whitespace per entry and drops empty entries and entries ending with ':'
    after trim (so a bare `cline:` from an unresolved token is skipped). Falls
    back to the default when filtering leaves nothing, so a misconfigured env
    can never produce an empty chain. Stdlib only — no new dependency.
    """
    raw = os.environ.get(env_var, '').strip()
    if not raw:
        return list(default)
    out = []
    for part in raw.split(','):
        part = part.strip()
        if part and not part.endswith(':'):
            out.append(part)
    return out if out else list(default)


_DEFAULT_THESIS_MODELS = _configured_models(
    'byteplus:deepseek-v4-flash',
    f"cline:{os.getenv('CLINE_THESIS_MODEL', os.getenv('CLINE_MODEL', ''))}",
    f"ninerouter:{os.getenv('NINEROUTER_THESIS_MODEL', 'thesis')}",
)
# THESIS_MODELS is the env-configured chain (THESIS_MODELS=a,b,c) — the single
# source both the batch + single-thesis cascades iterate. The legacy
# THESIS_MODEL/THESIS_FALLBACK/THESIS_FINAL_FALLBACK aliases below are derived
# from it for back-compat with code/tests that index them directly.
THESIS_MODELS = _parse_model_chain('THESIS_MODELS', _DEFAULT_THESIS_MODELS)
THESIS_MODEL = THESIS_MODELS[0]
THESIS_FALLBACK = THESIS_MODELS[1] if len(THESIS_MODELS) > 1 else THESIS_MODELS[0]
THESIS_FINAL_FALLBACK = THESIS_MODELS[-1] if len(THESIS_MODELS) > 1 else THESIS_MODELS[0]
# Budget for a SINGLE thesis (the per-iteration regeneration path). Sized for
# reasoning + answer, not the answer alone — see SELF_CRITIQUE_MAX_TOKENS below
# for the mechanism.
THESIS_SINGLE_MAX_TOKENS = 2500

# Self-critique gate: a reflection pass run AFTER a thesis passes structural
# validation and BEFORE code-gen, so flawed designs don't burn code-gen +
# validation compute. It enforces the DESIGN principles already in the role
# prompt — catching the failure modes a strict *backtest* validator cannot see
# and that have repeatedly required manual rejection on review (post-hoc
# mechanism, thesis↔logic contradiction, circular regime gate, look-ahead
# shape). It is NOT a performance predictor and never sees validation scores,
# so it cannot optimise toward the validator. Deliberately conservative (reject
# only on a clear fatal flaw; pass when in doubt) and fail-open (any LLM/parse
# error → pass, so a flaky API never starves the batch).
SELF_CRITIQUE_ENABLED = True
# Budget covers reasoning tokens, not just the answer. The opencode models emit
# their chain of thought into reasoning_content first; _chat_content reads only
# content, so a budget sized for the answer alone returns '' with
# finish_reason=length and the model is scored as a hard failure. Measured
# 2026-07-23: minimax-m3 fails this gate at 400, passes at 2000.
SELF_CRITIQUE_MAX_TOKENS = 2000
# INDEPENDENCE RULE (2026-08-09): the critique head must never equal the THESIS head.
# It silently did for an unknown period — .env pinned CRITIQUE_MODELS and THESIS_MODELS
# to the SAME byteplus:deepseek-v4-flash, so the generator graded its own theses, while
# this comment still described a deepseek-chat setup that no longer existed. .env is
# untracked, so the drift left no history and nothing failed loudly. Now glm-5.2 (a
# different family) with v4-flash demoted to fallback. Re-qualify any change with
# scripts/critique_control.py, and read its docstring first: the set proves ABSENCE OF
# OVER-REJECTION, it does not rank models (its one discriminating case is unstable at
# temperature 0). A gate that over-rejects starves generation silently, which is the
# expensive direction; picking a "smarter" grader on a 6-case sample is not supported.
#
# Historical: self-critique moved OFF gpt-oss:free 2026-06-30
# to de-conflict the single working free model: gpt-oss was triple-booked (code-gen +
# 527 self-critiques/day + meta-review) and 429ing, pushing code-gen onto the paid
# backstop. Self-critique is a SMALL prompt, so cheap-paid here is nearly free and it
# frees gpt-oss's free quota for the expensive code-gen prompt. deepseek-chat verified
# 4/4 on the control set (passes valid MR+macro, rejects circular+look-ahead, all
# parseable); nemotron:free FAILED (3/4 unparseable -> fail-open would disable the
# gate) and v4-flash over-rejected valid macro. gpt-oss:free is the free fallback on a
# rare deepseek miss, then fail open — so a hiccup can never starve the batch.
_DEFAULT_CRITIQUE_MODELS = _configured_models(
    'byteplus:deepseek-v4-flash',
    f"cline:{os.getenv('CLINE_CRITIQUE_MODEL', os.getenv('CLINE_MODEL', ''))}",
    f"ninerouter:{os.getenv('NINEROUTER_THESIS_MODEL', 'thesis')}",
)
# CRITIQUE_MODELS is the env-configured chain (CRITIQUE_MODELS=a,b,c). The
# self-critique gate iterates this list directly; SELF_CRITIQUE_MODELS and
# SELF_CRITIQUE_MODEL are kept as aliases for back-compat.
CRITIQUE_MODELS = _parse_model_chain('CRITIQUE_MODELS', _DEFAULT_CRITIQUE_MODELS)
SELF_CRITIQUE_MODELS = CRITIQUE_MODELS
SELF_CRITIQUE_MODEL = CRITIQUE_MODELS[0]
# Greedy decoding (temp 0) — this is a binary judgment gate, not a creative task,
# so we want the single most-likely verdict, not sampled variety. Verified to make
# every clear-cut control case fully consistent (8/8 across all genuine flaws and
# blessed regime detectors). NOTE: one narrow construct — a positive `.shift(n)`
# in a filter — remains a stubborn conservative over-reject (gpt-oss strongly
# priors `.shift` → look-ahead; a worked prompt example did NOT move it and risked
# anchoring, so it was dropped). That miss is in the safe direction (the idea just
# returns to the pool) and is accepted rather than over-tuned.
SELF_CRITIQUE_TEMPERATURE = 0.0

# Data-grounded thesis generation (2026-06-16): feed each item the instrument's
# MEASURED in-sample structure (autocorrelation, efficiency ratio, vol clustering,
# skew, calendar) so the model designs FOR the data instead of recalling a
# textbook pattern, and reject theses whose core assumption contradicts the
# measurement. Computed on the dev window only (no holdout leak) and fully
# fail-soft. Default OFF — the feature stays inactive until either explicitly
# enabled (FINGERPRINT_ENABLED=1) or chosen per-batch by the A/B controller below.
FINGERPRINT_ENABLED = os.environ.get('FINGERPRINT_ENABLED', '1') != '0'  # on by default — DRIVEN graduated 2026-06-18


def _fp_compact(instrument: str, granularity: str) -> str:
    """Compact measured-structure line for a batch item, or '' (fail-soft)."""
    if not FINGERPRINT_ENABLED:
        return ''
    try:
        import fingerprint
        return fingerprint.format_compact(
            fingerprint.compute_fingerprint(instrument, granularity or 'D'))
    except Exception:
        return ''


def _driven_for_batch(n: int, ratio: float) -> bool:
    """Deterministic Bresenham / largest-remainder split: returns True (DRIVEN) for
    `ratio` fraction of batches, kept temporally interleaved rather than blocked into
    long single-arm runs. ratio=0.5 -> N,D,N,D...  ratio=0.7 -> 7 DRIVEN / 3 NORMAL
    per 10 (spread F,T,T,F,T,T,F,T,T,T). Clamped to [0,1]."""
    ratio = min(1.0, max(0.0, ratio))
    return (int((n + 1) * ratio) - int(n * ratio)) > 0


def _ab_select_fingerprint_arm() -> bool:
    """A/B controller (active only when AB_TEST_FINGERPRINT=1): alternate the
    data-grounded arm per batch via a persistent counter and append the arm +
    timestamp to .ab_test/ledger.jsonl, so the two arms' IS-score distributions
    can be split by created_at afterward. Returns the arm (True=fingerprint on).
    Best-effort — any error defaults the batch to the OFF (control) arm."""
    import json
    from pathlib import Path
    n = 0
    try:
        d = Path(__file__).parent / '.ab_test'
        d.mkdir(exist_ok=True)
        cf = d / 'counter'
        try:
            n = int(cf.read_text().strip())
        except Exception:
            n = 0
        cf.write_text(str(n + 1))
        try:
            ratio = float(os.environ.get('AB_DRIVEN_RATIO', '0.5'))
        except Exception:
            ratio = 0.5
        ratio = min(1.0, max(0.0, ratio))
        driven = _driven_for_batch(n, ratio)
        with open(d / 'ledger.jsonl', 'a') as f:
            f.write(json.dumps({'batch': n, 'arm': 'on' if driven else 'off',
                                'ratio': ratio,
                                'start': datetime.utcnow().isoformat()}) + '\n')
    except Exception:
        return False
    return driven


# ─── Generic chain-head A/B (AB_TEST_CHAIN) ────────────────────────────────────
#
# Swaps the HEAD of one model chain per batch and tags every candidate that reaches
# validation with the arm that produced it. Chain-generic by design: THESIS_MODELS,
# CODEGEN_MODELS and CRITIQUE_MODELS all parse through _parse_model_chain, so the swap
# is one code path — only the outcome metric is chain-specific, and that lives in the
# analysis, not here.
#
# Separate from the AB_TEST_FINGERPRINT controller above, which tests a generation
# PACKAGE rather than a model id. Both can be off; running both at once is untested and
# would confound two factors, so _ab_chain_arm refuses if the fingerprint A/B is live.
#
# Design constraints (see .scratch/thesis-ab/map.md):
#   - PAIRED SEEDS. Consecutive batches form a twin sharing one seed, so both arms see an
#     identical instrument/constraint/timeframe schedule and schedule variance cancels.
#   - EXPLICIT strategy_id in every sidecar row. The arm must never be recoverable only
#     by created_at inference — that is the defect in the fingerprint A/B's ledger.
#   - PROVENANCE. Each row stamps the git sha/branch and the model id actually resolved,
#     so a mid-run code change is DETECTABLE at analysis time instead of silent.
#   - FAIL CLOSED to the control arm, loudly.
_AB_CHAIN_VARS = {
    'thesis': 'THESIS_MODELS',
    'codegen': 'CODEGEN_MODELS',
    'critique': 'CRITIQUE_MODELS',
}
_AB_DIR = Path(__file__).parent / '.ab_test'
_AB_PAIR_SEED: Optional[int] = None      # set when active; pins _asset_mode_for rotation
_AB_STATE: Dict[str, Any] = {}           # this batch's arm, for _ab_tag_candidate


def _ab_git_provenance() -> Dict[str, str]:
    """(sha, branch) of the tree the loop is running from — best effort, never raises.

    The research loop runs the WORKING TREE, so an edit or a checkout mid-run changes the
    code generating the sample. Stamping it makes that detectable rather than silent.
    """
    out = {'git_sha': '', 'git_branch': ''}
    try:
        import subprocess
        root = str(Path(__file__).parent)
        for key, cmd in (('git_sha', ['git', 'rev-parse', 'HEAD']),
                         ('git_branch', ['git', 'rev-parse', '--abbrev-ref', 'HEAD'])):
            r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                out[key] = r.stdout.strip()
    except Exception:
        pass
    return out


# The files that decide what a candidate IS: the generation loop, the thesis prompt it
# splices, the steering that picks the slot, and the validator that scores the endpoint.
# A change to any of them mid-run is a different experiment; a change anywhere else
# (fix_runner, simulators, the prop guard) is not.
_AB_GEN_PATHS = (
    'auto_research.py', 'validator.py', 'thesis.md', 'fingerprint.py', 'steering.py',
    'reason_codes.py', 'strategy_honesty.py', 'data_fetcher.py', 'macro_fetcher.py',
    'fred_events.py', 'supplementary_data.py',
)


def _ab_gen_fingerprint() -> Dict[str, Any]:
    """Hash the CONTENT of the generation-path files, not the commit they came from.

    WHY THIS EXISTS AND git_sha DOES NOT SUFFICE. The loop runs the working tree, and
    two of these files are routinely dirty: meta_review.run_meta_review() REWRITES the
    <!-- RESEARCH_PHASE --> directives inside thesis.md while the loop is running, and
    that edit is never committed. A sha stamp therefore reports 'unchanged' across a
    prompt rewrite that changed what every later batch was asked to produce. Content
    hashing catches it; the sha cannot, in principle.

    Returns {'gen_sha': <hash over the set>, 'gen_files': {path: <per-file hash>}} so a
    split can be localised to the file that moved instead of merely detected.
    """
    import hashlib
    root = Path(__file__).parent
    per_file: Dict[str, str] = {}
    for rel in _AB_GEN_PATHS:
        try:
            per_file[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()[:12]
        except Exception:
            per_file[rel] = 'MISSING'
    joined = '\n'.join(f'{k}:{v}' for k, v in sorted(per_file.items()))
    return {'gen_sha': hashlib.sha256(joined.encode()).hexdigest()[:12],
            'gen_files': per_file}


def _ab_chain_arm() -> Optional[Dict[str, Any]]:
    """Select this batch's arm for AB_TEST_CHAIN and pin the paired seed.

    Returns the arm dict, or None when the A/B is not active. Any failure falls back to
    the CONTROL arm and says so — a silently mis-armed batch is worse than a lost one.
    """
    chain = os.environ.get('AB_TEST_CHAIN', '').strip().lower()
    if not chain:
        return None
    if chain not in _AB_CHAIN_VARS:
        print(f"  [A/B] IGNORED — AB_TEST_CHAIN={chain!r} is not one of "
              f"{sorted(_AB_CHAIN_VARS)}", flush=True)
        return None
    if os.environ.get('AB_TEST_FINGERPRINT', '0') == '1':
        print("  [A/B] REFUSING — AB_TEST_FINGERPRINT is also active; running two A/Bs "
              "at once confounds both. Disable one.", flush=True)
        return None

    control = os.environ.get('AB_ARM_CONTROL', '').strip()
    challenger = os.environ.get('AB_ARM_CHALLENGER', '').strip()
    if not control or not challenger:
        print("  [A/B] REFUSING — AB_ARM_CONTROL and AB_ARM_CHALLENGER must both be set "
              "(no model id is hardcoded in the harness).", flush=True)
        return None

    batch, failed_closed = 0, False
    try:
        _AB_DIR.mkdir(exist_ok=True)
        cf = _AB_DIR / f'counter-{chain}'
        try:
            batch = int(cf.read_text().strip())
        except Exception:
            batch = 0
        cf.write_text(str(batch + 1))
    except Exception as exc:
        failed_closed = True
        print(f"  [A/B] counter FAILED ({exc}) — falling back to CONTROL arm", flush=True)

    # Twin batches share a seed: (0,1) -> 0, (2,3) -> 1, ... Even batch = control.
    is_challenger = (not failed_closed) and (batch % 2 == 1)
    arm = {
        'chain': chain,
        'arm': 'challenger' if is_challenger else 'control',
        'model': challenger if is_challenger else control,
        'batch': batch,
        'seed': batch // 2,
        'failed_closed': failed_closed,
    }
    arm.update(_ab_git_provenance())
    arm.update(_ab_gen_fingerprint())

    global _AB_PAIR_SEED
    _AB_PAIR_SEED = arm['seed']

    try:
        with open(_AB_DIR / f'ledger-{chain}.jsonl', 'a') as f:
            f.write(json.dumps({**arm, 'start': datetime.utcnow().isoformat()}) + '\n')
    except Exception as exc:
        print(f"  [A/B] ledger write failed ({exc}) — continuing", flush=True)
    return arm


def _ab_apply_arm(arm: Dict[str, Any]) -> None:
    """Put the arm's model at the HEAD of its chain, in-process for this batch."""
    var = _AB_CHAIN_VARS[arm['chain']]
    chain = [m for m in globals().get(var, []) if m != arm['model']]
    globals()[var] = [arm['model']] + chain
    if arm['chain'] == 'thesis':                 # keep the derived aliases consistent
        globals()['THESIS_MODEL'] = arm['model']
        globals()['DEFAULT_MODEL'] = arm['model']
    _AB_STATE.clear()
    _AB_STATE.update(arm)
    print(f"  [A/B] {arm['chain']} batch {arm['batch']} → {arm['arm'].upper()} "
          f"({arm['model']}), seed {arm['seed']}"
          + ("  [FAILED CLOSED]" if arm['failed_closed'] else ''), flush=True)


def _ab_tag_candidate(strategy_id: str, instrument: str = '', timeframe: str = '') -> None:
    """Record which arm produced this candidate. Best effort — never breaks the loop.

    Called once the candidate has been through validation, so the sidecar contains
    exactly the population the analysis joins to validation_results.
    """
    if not _AB_STATE or not strategy_id:
        return
    try:
        _AB_DIR.mkdir(exist_ok=True)
        row = {
            'strategy_id': strategy_id,          # the join key — never inferred
            'chain': _AB_STATE['chain'],
            'arm': _AB_STATE['arm'],
            'model': _AB_STATE['model'],
            'batch': _AB_STATE['batch'],
            'seed': _AB_STATE['seed'],
            'instrument': instrument,
            'timeframe': timeframe,
            'git_sha': _AB_STATE.get('git_sha', ''),
            'git_branch': _AB_STATE.get('git_branch', ''),
            # content hash of the generation path — catches the uncommitted thesis.md
            # directive rewrite that git_sha is blind to. Digest only; the per-file map
            # is in the batch's ledger row, so a split can be localised without paying
            # for eleven hashes on every candidate.
            'gen_sha': _AB_STATE.get('gen_sha', ''),
            'tagged_at': datetime.utcnow().isoformat(),
        }
        with open(_AB_DIR / f"tags-{_AB_STATE['chain']}.jsonl", 'a') as f:
            f.write(json.dumps(row) + '\n')
    except Exception as exc:
        print(f"  [A/B] tag write failed for {strategy_id} ({exc})", flush=True)


# Code generation: gpt-oss:free leads (the proven code formatter), then PAID deepseek-chat
# catches any 429 overflow. nemotron-3-super:free was dropped 2026-06-30 — on the complex
# codegen prompt it parse-failed ~97% (returns no ```python block), wasting the first
# attempt every call; it works on SIMPLE prompts but not this one.
# ORDER FLIPPED 2026-07-02: gpt-oss:free was primary but OpenRouter's global
# free-tier capacity 429'd it on ~60% of code calls (measured 157/262 served by
# ark), so most calls paid a wasted attempt + retry latency before landing on
# BytePlus anyway. ark is flat-rate (coding plan), fast (~6s) and reliable —
# it's now PRIMARY; free gpt-oss is the backstop if BytePlus has an outage.
_DEFAULT_CODEGEN_MODELS = _configured_models(
    'byteplus:ark-code-latest',
    f"cline:{os.getenv('CLINE_CODEGEN_MODEL', os.getenv('CLINE_MODEL', ''))}",
    f"ninerouter:{os.getenv('NINEROUTER_CODEGEN_MODEL', 'codegen')}",
)
# CODEGEN_MODELS is the env-configured chain (CODEGEN_MODELS=a,b,c). The
# code-gen retry loop iterates this list; CODE_FALLBACK_MODELS is kept as an
# alias for back-compat.
CODEGEN_MODELS = _parse_model_chain('CODEGEN_MODELS', _DEFAULT_CODEGEN_MODELS)
CODE_FALLBACK_MODELS = CODEGEN_MODELS

# Creative constraints rotated per iteration — forces structural diversity in thesis proposals.
# Wild mode (every 8th iteration) overrides the constraint with an open exploration directive.
# 2026-06-29 rebalanced: the pool was ~40% autocorrelation/efficiency-ratio mean-reversion
# because three constraints (forced-statistical, bar-range, autocorr-regime) all pushed it.
# Those are replaced with directional-momentum, cross-market, and volatility/calendar gates
# that push AWAY from the autocorr/reversion monoculture.
# ── Category instruction files: categories/<name>.md ───────────────────────
# Each generation category (macro, calendar, event, pair, asset, standard, wild)
# has ONE md: a `## CONSTRAINT` block (the text the generator injects) + a
# `## GUIDANCE` block (reference prose, spliced into the thesis prompt). The md
# is the single source a maintainer edits; the code loads it once per process and
# FAILS SOFT to the inline fallback below, so a missing/edited file can never
# break the live loop. Dynamic categories (macro/asset) carry {instrument}/{cols}/
# {chosen} tokens replaced by str.replace — NOT .format(), because asset's prose
# has literal braces. Added 2026-07-08.
_CATEGORY_DIR = Path(__file__).parent / 'categories'
_CATEGORY_CACHE: Dict[str, Optional[dict]] = {}


def _parse_category_md(text: str) -> dict:
    c = re.search(r'(?ms)^##\s+CONSTRAINT\s*$(.*?)(?=^##\s+GUIDANCE\s*$|\Z)', text)
    g = re.search(r'(?ms)^##\s+GUIDANCE\s*$(.*)\Z', text)
    return {'constraint': (c.group(1).strip() if c else ''),
            'guidance': (g.group(1).strip() if g else '')}


def _load_category(name: str) -> Optional[dict]:
    if name in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[name]
    result = None
    try:
        p = _CATEGORY_DIR / f'{name}.md'
        if p.exists():
            parsed = _parse_category_md(p.read_text())
            if parsed['constraint']:
                result = parsed
    except Exception:
        result = None
    _CATEGORY_CACHE[name] = result
    return result


def _category_constraint(name: str, **tokens) -> str:
    """CONSTRAINT text for a category from its md, falling back to the inline
    default. Dynamic tokens (instrument/cols/chosen) are replaced literally."""
    cat = _load_category(name)
    text = cat['constraint'] if cat else _FALLBACK_CONSTRAINTS.get(name, '')
    for k, v in tokens.items():
        text = text.replace('{' + k + '}', str(v))
    return text


def _category_list(name: str) -> list:
    """A '---'-delimited CONSTRAINT block as an ordered list (standard rotation)."""
    return [s.strip() for s in _category_constraint(name).split('\n---\n') if s.strip()]


def _category_guidance(name: str) -> str:
    cat = _load_category(name)
    return cat['guidance'] if cat else ''


# ── Inline fallbacks — byte-identical to the md CONSTRAINT blocks ───────────
_FALLBACK_WILD = (
    "WILD MODE: Ignore conventional strategy families. "
    "Propose something structurally different — unusual timeframe, "
    "non-standard entry logic, exotic exit rule."
)
_FALLBACK_MACRO = (  # {instrument} {cols} tokens
    "MACRO MODE: design a strategy whose edge is driven by macro data — rate "
    "differentials, carry, central-bank policy divergence, real-yield moves, or "
    "DXY regime. entry_condition or filter_condition MUST reference one or more "
    "of these EXACT macro columns, which are the ONLY ones available for "
    "{instrument}: {cols}. Do NOT reference any macro column outside that list "
    "— inventing a column name will fail the strategy. "
    "IMPORTANT: macro values arrive with their real-world PUBLICATION lags "
    "(daily rates/yields ~1 day late, the dollar index ~1 week late, CPI and "
    "other monthly series ~6 weeks late). Same-day macro reactions are NOT "
    "observable — design the edge around persistent macro conditions and "
    "slow-moving differentials, not immediate responses to today's data. "
    "This is a macro-archetype strategy."
)
_FALLBACK_ASSET = (  # {instrument} {chosen} tokens; the literal braces are intentional
    "ASSET MODE for {instrument} this visit: design a strategy whose "
    "edge comes from THIS ONE specific calendar/session/seasonal "
    'feature: "{chosen}". The parenthesised expression in that feature '
    "is the date pattern to implement — copy it literally into the "
    "ENTRY_CONDITION (the calendar pattern is the entry TRIGGER) using "
    "pd.to_datetime(df['date']).dt to extract month/day_of_week/day_of_month/hour. "
    "The filter_condition MUST be a SEPARATE price/volatility regime gate "
    "(e.g. an ATR-vs-median vol regime, or trend-strength) — it must NOT "
    "restate the calendar pattern; a filter that repeats the entry's date "
    "condition is a circular gate and is REJECTED. Do NOT reference "
    "event-tag columns like 'cot_report_change', 'china_cpi_release', "
    "'event_impact', or any column not in {open, high, low, close, "
    "date} (plus the calendar columns dow/cal_month/tdom/tdom_left/turn_of_month "
    "if you set archetype='calendar' — preferred for seasonal/flow edges, robust "
    "vs df.index.dayofweek which crashes; or macro columns if you set archetype='macro' — "
    "and note macro values arrive publication-lagged: yields ~1 day, "
    "DXY ~1 week, CPI ~6 weeks, so no same-day macro reactions). "
    "The asset concept MUST drive the edge — plain technical indicators "
    "(RSI, MACD, SMA crossovers, ATR breakouts, skewness, autocorrelation) "
    "used in ISOLATION are NOT acceptable; they may appear as supporting "
    "filters but the asset-specific concept above must be the edge."
)
_FALLBACK_CALENDAR = (
    "CALENDAR/SEASONAL: design a TWO-SIDED edge from a dated institutional flow with a "
    "NAMED origin (month-end index/pension rebalancing, turn-of-month retirement inflows, "
    "options-expiry positioning, day-of-week liquidity). Build it from the calendar columns "
    "(dow, cal_month, tdom, tdom_left, turn_of_month) — NOT df.index. Name the flow and a "
    "falsifiable window; do NOT fish for the best weekday. The calendar window IS the regime "
    "gate (no separate price detector needed). Aim for balanced long/short occurrence."
)
_FALLBACK_EVENT = (
    "EVENT-TIMING: build a TWO-SIDED edge whose ENTRY or FILTER is gated on the US "
    "economic-release calendar using the injected columns days_to_event, "
    "days_since_event, event_window (TIMING ONLY — there is NO surprise/actual value). "
    "E.g. fade range extremes into pre-release compression (days_to_event<=2), or trade "
    "the post-release reaction when event_window==1 with a price/vol entry. The entry or "
    "filter MUST reference at least one of days_to_event / days_since_event / event_window "
    "by name. A thesis that does NOT reference an event column is OFF-SPEC and will be "
    "DISCARDED — do NOT fall back to a price-only strategy. Design every window for DAILY "
    "bars (these columns are day-resolution)."
)

_FALLBACK_CREATIVE = [
    "Must avoid all moving-average crossover logic. Use price-relative or range-based entry instead.",
    "Entry must be a directional momentum/continuation signal — trade WITH the move, not a fade. "
    "Do NOT use mean-reversion, skewness, or autocorrelation.",
    "Use only day-of-week or time-of-session effects — no rolling indicator windows.",
    "Build a spread strategy using the open-to-close range as the signal — no second instrument needed.",
    "Exit must be time-boxed: a mechanism exit reading the quantity the entry is built on "
    "(indicator cross, level reclaimed, spread reverted) PLUS a hard bar-count timeout. "
    "Do not add a separate price/ATR stop — the backtest injects one anyway.",
    "Entry only on breakout above/below a quantile of the last N bars' range.",
    "Strategy must be mean-reverting in entry but momentum-confirming in filter.",
    "Use an asymmetric parameter grid: longs and shorts use different lookbacks.",
    "Cross-market PAIR: trade the SPREAD/RATIO between the instrument and a SECOND tradeable "
    "OANDA instrument (e.g. XAU_USD vs XAG_USD, EUR_USD vs EUR_JPY, AUD_USD vs XCU_USD). You "
    "MUST set the \"instrument2\" field to that second instrument's OANDA symbol "
    "(INSTRUMENT_UNDERSCORE format, a REAL instrument, never a ratio like ETH_BTC) — the entry/exit "
    "reference close_leg2 / the spread. A pair thesis WITHOUT the instrument2 field is DISCARDED. "
    "(For a macro FACTOR instead of a pair — DXY, a rate differential — use the macro archetype, not this.)",
    "Gate the edge with a volatility regime (realized vol or ATR vs its median) or a calendar "
    "window — do NOT gate with autocorrelation or efficiency ratio.",
    # Event-timing MOVED to a dedicated forced slot (categories/event.md, 2026-07-08):
    # as 1-of-11 creative constraints the model ignored it ~90% of the time.
    # The Cross-market PAIR entry lives in categories/pair.md and is appended below.
]
# Split fallback: the standard rotation (generic price constraints) vs the pair
# constraint, which has its own category file (categories/pair.md).
_FALLBACK_STANDARD = [c for c in _FALLBACK_CREATIVE if 'Cross-market PAIR' not in c]
_FALLBACK_PAIR = next((c for c in _FALLBACK_CREATIVE if 'Cross-market PAIR' in c), '')
_FALLBACK_NNFX = (
    "NNFX MODE: use two mandatory independent layers: one direction baseline and one momentum "
    "confirmation. Baselines: EMA slope, Donchian midpoint slope, vectorized linear-regression "
    "slope, or Kijun-Sen; do not default to Kijun. Confirmations: Fisher transform, ROC, CCI, "
    "Stochastic, or Awesome Oscillator; do not default to MACD. An optional volatility/structure "
    "filter may use Bollinger-width, realized-volatility, or ATR percentile only when it does not "
    "starve entries. Default example: EMA slope + Fisher confirmation + optional Bollinger-width "
    "regime. Exit on baseline cross or confirmation reversal. The validator owns the ATR stop via "
    "compute_returns_with_stop; generated code must not implement ATR/Chandelier trailing-stop "
    "state, per-bar position loops, or entry-price tracking. Require orthogonal indicator families, "
    "deterministic vectorized code, at most 4 tunable parameters, and at most 200 original grid "
    "combinations. Never use .rolling(...).apply(). This is a standard-archetype strategy."
)
_FALLBACK_ACADEMIC = (
    "ACADEMIC RECALL MODE: draw on documented pre-2020 academic finance literature. Your assigned "
    "anomaly for this slot is: {anomaly}. Build the thesis on THAT anomaly — a thesis that is not a "
    "time-series implementation of it is OFF-SPEC and will be DISCARDED. The rationale MUST begin "
    "with the literal prefix `ACADEMIC(<anomaly name>): ` and then state in one sentence why the "
    "anomaly exists (the behavioural or structural mechanism) and its known decay or regime "
    "dependency. SINGLE INSTRUMENT ONLY — there is no cross-section, so do not propose ranking a "
    "universe, decile sorts, long-short baskets, betting-against-beta or per-name earnings drift; "
    "implement the TIME-SERIES form. If the anomaly is macro-driven (carry, value/PPP, policy "
    "divergence, real yields) you must reference one or more of these EXACT macro columns, the ONLY "
    "ones available for {instrument}: {cols} — inventing a column name will fail the strategy — and "
    "declare a macro-archetype strategy; macro values carry real-world publication lags, so design "
    "around persistent conditions, not same-day reactions. If it is price-based, use OHLC only and "
    "declare a standard-archetype strategy. Encode the documented regime dependency as the "
    "filter_condition and prefer a two-sided edge. SIGNAL STARVATION is how this category fails: a "
    "slow anomaly (12-1 momentum, long-term reversal, PPP value, policy divergence) is a STATE — put "
    "it in filter_condition and put a PRICE/VOL trigger that fires often inside that state in "
    "entry_condition, never two slow selective conditions multiplied together, and keep the total "
    "lookback well under half the available history. HARD LIMITS: deterministic vectorized code, at "
    "most 4 tunable parameters, at most 200 original grid combinations, never .rolling(...).apply(). "
    "The validator owns the ATR stop via compute_returns_with_stop; generated code must not "
    "implement trailing-stop state, per-bar position loops, or entry-price tracking."
)
_FALLBACK_CONSTRAINTS = {
    'standard': '\n---\n'.join(_FALLBACK_STANDARD),
    'pair': _FALLBACK_PAIR,
    'calendar': _FALLBACK_CALENDAR,
    'event': _FALLBACK_EVENT,
    'wild': _FALLBACK_WILD,
    'macro': _FALLBACK_MACRO,
    'asset': _FALLBACK_ASSET,
    'nnfx': _FALLBACK_NNFX,
    'academic': _FALLBACK_ACADEMIC,
}
# Public rotation list = standard.md items + the pair.md constraint (same 10
# entries as before, now sourced from categories/*.md with inline fallback).
_CREATIVE_CONSTRAINTS = _category_list('standard') + [_category_constraint('pair')]

# Regime detectors rotated per iteration. A menu in the prompt is not enough —
# the thesis model anchors hard on ADX. Forcing one specific detector per
# iteration is what actually diversifies the regime gates across the pool.
_REGIME_DETECTORS = [
    "ADX(14) — directional trend-strength index",
    "lag-1 return autocorrelation over 30-60 bars (negative = ranging, positive = trending)",
    "realized volatility relative to its own 60-bar median",
    "fast/slow MA separation: abs(EMA(20) - EMA(50)) / ATR(14)",
    "MA-slope magnitude: abs(SMA(50) - SMA(50).shift(10)) / ATR(14)",
    "distance from mean: abs(close - SMA(50)) / ATR(14) (small = ranging, large = extended)",
    "efficiency ratio: net move / sum of absolute bar moves over 20 bars",
]

# Forced on a rotation slot (every 3rd non-wild iteration) so every batch
# produces some macro strategies. A passive "macro data is available" note in
# thesis.md is not enough — the model anchors on price-only strategies the same
# way it anchored on ADX. This replaces the creative constraint for that slot.
#
# The macro columns available are INSTRUMENT-SPECIFIC (see macro_fetcher
# _INSTRUMENT_COLS). A generic menu makes the model invent column names that
# don't get injected — e.g. nz_rate / nzr_rate on NZD pairs — which then
# KeyError at signal-check. So the constraint lists the EXACT columns for the
# instrument and forbids any others.
def _macro_constraint_for(instrument: str) -> str:
    # Wrapper text lives in categories/macro.md ({instrument}/{cols} tokens); the
    # per-instrument column list stays here (macro_fetcher is the source of truth).
    from macro_fetcher import list_available_columns
    cols = sorted(list_available_columns(instrument).keys())
    return _category_constraint('macro', instrument=instrument, cols=cols)


# ACADEMIC RECALL MODE (2026-08-09): pin ONE documented anomaly per slot and ask
# for its TIME-SERIES form. The anomaly is rotated here rather than chosen by the
# model for the same reason nnfx.md has to say "do not default to Kijun-Sen" — an
# unpinned model collapses to momentum on nearly every call.
#
# Cross-sectional anomalies are absent BY CONSTRUCTION: every sleeve trades one
# instrument, so cross-sectional momentum/value, betting-against-beta, the
# idiosyncratic-vol puzzle, accruals and PEAD cannot be expressed at all. The
# volatility risk premium survives only as a realized-vol term-structure proxy —
# there is no options data in this pipeline.
_ACADEMIC_ANOMALIES = [
    "Time-Series Momentum (12-1)",
    "Short-Term Reversal",
    "FX Carry Trade",
    "Time-Series Breakout (Managed Futures Trend Premium)",
    "Real-Exchange-Rate Value (PPP Deviation)",
    "Low-Volatility Effect (Time-Series Form)",
    "Monetary-Policy Divergence",
    "Volatility Risk Premium Proxy (Realized-Vol Term Structure)",
    "Long-Term Reversal",
    "Turn-of-the-Month Flow",
]


# The `ACADEMIC(<name>): ` prefix is this category's ONLY attribution — nothing
# else in strategies/validation_results records which anomaly produced a row. So
# the model's spelling of the name IS the join key, and it drifts: measured over
# the first 330 academic gens (2026-08-09..14), one anomaly appeared under three
# labels ("Time-Series Momentum", "Time-Series Momentum (12-1)", "Time-Series
# Momentum 12-1"), Low-Vol and VRP under two each, and one row dropped the colon
# entirely (`ACADEMIC(Short-term reversal) ...`), which hides it from any query
# keying on the documented prefix. Per-anomaly conversion rates were therefore
# unusable — the thing the category exists to measure.
#
# Canonicalise at write time, not in the query: the stored prose is what every
# later reader (meta_review, the shelf, an ad-hoc sqlite count) sees.
# Depth-counted rather than `\(([^)]*)\)`: two canonical names carry a nested
# parenthetical ("Time-Series Momentum (12-1)"), so a first-close-paren match
# truncates exactly the labels most in need of normalising.
_ACADEMIC_OPEN_RE = re.compile(r'^\s*ACADEMIC\s*\(', re.IGNORECASE)


def _split_academic_prefix(rationale: str):
    """(anomaly label, rest of rationale) for an `ACADEMIC(...)` prefix, or None.
    Tolerates a missing colon after the closing paren — one observed row dropped
    it, and dropping the row is worse than accepting the typo."""
    m = _ACADEMIC_OPEN_RE.match(rationale or '')
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(rationale) and depth:
        if rationale[i] == '(':
            depth += 1
        elif rationale[i] == ')':
            depth -= 1
        i += 1
    if depth:                       # unbalanced — not a prefix we can trust
        return None
    rest = rationale[i:].lstrip()
    if rest.startswith(':'):
        rest = rest[1:].lstrip()
    return rationale[m.end():i - 1], rest


def _anomaly_key(name: str) -> str:
    """Alphanumeric-lowercase fold. Drops case, hyphens, spaces and parentheses,
    so "Time-Series Momentum 12-1" and "Time-Series Momentum (12-1)" collapse to
    the same key while short-term and long-term reversal stay distinct."""
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _canonical_anomaly(name: str) -> Optional[str]:
    """The canonical _ACADEMIC_ANOMALIES spelling for a model-written label, or
    None if it matches none of them. A truncated label (the model keeps the head
    and drops the parenthetical gloss) is matched by prefix; no canonical key is
    a prefix of another, so the match is unambiguous."""
    key = _anomaly_key(name)
    if not key:
        return None
    for canon in _ACADEMIC_ANOMALIES:
        ckey = _anomaly_key(canon)
        if key == ckey or ckey.startswith(key) or key.startswith(ckey):
            return canon
    return None


def _canonical_academic_rationale(rationale: str) -> str:
    """Rewrite a rationale's `ACADEMIC(...)` prefix to the canonical anomaly name
    and the documented `): ` separator. Non-academic rationales, and academic
    ones naming an anomaly outside the rotation, are returned unchanged — an
    off-rotation name is a signal worth keeping visible, not something to coerce
    onto the nearest neighbour."""
    split = _split_academic_prefix(rationale)
    if split is None:
        return rationale
    label, rest = split
    canon = _canonical_anomaly(label)
    if canon is None:
        return rationale
    return f'ACADEMIC({canon}): {rest}'


# Carry and PPP value are defined by a CURRENCY rate/price differential — they are
# meaningless on gold, crude or an equity index. Measured on the first dry run of
# this category: 2 of 4 academic slots in a 31-batch drew "FX Carry Trade" for
# WTICO_USD and NAS100_USD, which the model can only answer off-spec.
_ACADEMIC_FX_ONLY = frozenset({
    "FX Carry Trade",
    "Real-Exchange-Rate Value (PPP Deviation)",
})
# ISO codes only. XAU/XAG/XCU and the index/commodity/crypto prefixes are absent
# by construction, so XAU_USD, DE30_EUR, NAS100_USD and BTC_USD all read non-FX.
_FX_CODES = frozenset({
    'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'NZD', 'CAD', 'SEK', 'NOK',
    'DKK', 'SGD', 'HKD', 'MXN', 'ZAR', 'TRY', 'PLN', 'CZK', 'HUF', 'CNH',
})


def _is_fx_pair(instrument: str) -> bool:
    parts = (instrument or '').upper().split('_')
    return len(parts) == 2 and all(p in _FX_CODES for p in parts)


# Being a currency pair is necessary but NOT sufficient: carry needs a FOREIGN
# policy rate or bond yield and PPP value needs a FOREIGN CPI, and macro_fetcher
# does not carry both legs for every pair. Measured 2026-08-09 over the 9 FX pairs
# in the pool: NZD_USD and USD_CHF have no foreign rate/yield, AUD_USD and NZD_USD
# have no foreign CPI. Assigning the anomaly anyway hands the model a column list
# that cannot express it, and the documented result is an INVENTED column name
# (`nz_rate`/`nzr_rate` on NZD pairs) that KeyErrors at signal-check.
def _has_foreign(cols, suffixes) -> bool:
    return any(c.endswith(suffixes) and not c.startswith(('us', 'fed', 'dxy'))
               for c in cols)


_ACADEMIC_DATA_REQS = {
    "FX Carry Trade": lambda cols: _has_foreign(cols, ('_rate', '10y')),
    "Real-Exchange-Rate Value (PPP Deviation)": lambda cols: _has_foreign(cols, ('_cpi',)),
}

# An anomaly that only works on ONE timeframe must be gated on that timeframe
# being ENABLED, not granted an override. Long-term reversal needs a 3-5 year
# lookback, which eats most of the sample on daily bars — so it is pinned to
# weekly below. That pin was unconditional and, because W had been dropped from
# steering.md's rotation, it silently reintroduced weekly bars the moment the
# rotation fix (2026-08-09) made this anomaly reachable for the first time. A
# category must never quietly overrule the steering timeframe: if the timeframe
# it needs is off, the anomaly is simply not expressible right now, exactly like
# carry without a foreign rate. Restore W to the rotation and it returns on its
# own.
_ACADEMIC_TF_REQS = {
    "Long-Term Reversal": 'W',
}


def _academic_anomalies_for(instrument: str, timeframes=None) -> list:
    """The anomalies expressible on this instrument, and on the timeframes the
    active rotation actually offers.

    Three gates: the FX-only forms need a currency pair (carry on gold is
    meaningless); they additionally need the specific macro column the anomaly is
    DEFINED by, which varies per pair; and a timeframe-pinned form needs its
    timeframe to be enabled. `timeframes=None` disables only the last gate, for
    callers that are asking about the instrument alone.
    """
    try:
        from macro_fetcher import list_available_columns
        cols = set(list_available_columns(instrument).keys())
    except Exception:
        cols = set()                      # fail-soft: drop the data-gated forms
    out = []
    for a in _ACADEMIC_ANOMALIES:
        if a in _ACADEMIC_FX_ONLY:
            if not _is_fx_pair(instrument):
                continue
            req = _ACADEMIC_DATA_REQS.get(a)
            if req and not req(cols):
                continue
        need_tf = _ACADEMIC_TF_REQS.get(a)
        if need_tf and timeframes is not None and need_tf not in set(timeframes):
            continue
        out.append(a)
    return out


def _academic_constraint_for(instrument: str, n: int, timeframes=None) -> str:
    """CONSTRAINT for one academic-recall slot: the pinned anomaly plus the
    instrument's EXACT macro columns (a macro-driven anomaly that invents a column
    name KeyErrors at signal-check — same failure the macro category guards).
    Wrapper text lives in categories/academic.md.

    `n` is a CONTINUOUS count of academic slots, NOT the iteration index and NOT
    a per-batch counter. Two starvation bugs have come from getting this wrong:

    1. Indexed by iteration i: the slot fires on i%6==1 so every academic i is
       ≡1 mod 6, and (i-1)%10 reached only the 5 EVEN entries — half the list
       unreachable.
    2. Indexed by a counter that RESET EACH BATCH (fixed 2026-08-09): a
       20-iteration batch fires exactly 4 academic slots (i=1,7,13,19), so n
       never exceeded 3 and only list positions 0-3 were reachable. Rendered
       over 60 batches: momentum/reversal/breakout 60 each, low-vol 44, carry
       14, PPP 2, and the last FOUR anomalies drawn ZERO times, ever. Hence the
       persistent counter in _academic_rotation_offset — n must carry across
       batch boundaries or the tail of the list is dead.

    Indexes the FULL anomaly list and then skips FORWARD to the next form this
    instrument can express, rather than indexing a pre-filtered pool. Filtering
    first loses list position, so a shorter pool (non-FX drops 2 entries) walks
    at a different rate and distorts the distribution — that is what made carry
    14 and PPP 2 while their neighbours got 60.
    """
    from macro_fetcher import list_available_columns
    cols = sorted(list_available_columns(instrument).keys())
    expressible = set(_academic_anomalies_for(instrument, timeframes))
    anomaly = None
    for step in range(len(_ACADEMIC_ANOMALIES)):
        cand = _ACADEMIC_ANOMALIES[(n + step) % len(_ACADEMIC_ANOMALIES)]
        if cand in expressible:
            anomaly = cand
            break
    if anomaly is None:                  # every form gated out — cannot happen
        anomaly = _ACADEMIC_ANOMALIES[n % len(_ACADEMIC_ANOMALIES)]
    return _category_constraint('academic', anomaly=anomaly,
                                instrument=instrument, cols=cols)


# Persisted so the anomaly walk survives a batch boundary — see the numbered
# starvation bugs in _academic_constraint_for. Stores the NEXT index to hand out,
# advanced by however many slots a batch actually consumed, so no stride is ever
# multiplied against the list length and the residue-aliasing class cannot recur.
_ACADEMIC_ROTATION_FILE = Path(__file__).parent / '.academic_rotation'


def _academic_rotation_offset() -> int:
    """Next anomaly index. Fail-soft to 0 — a lost counter costs coverage, not a
    crash, and the walk re-converges as it advances."""
    try:
        return max(0, int(_ACADEMIC_ROTATION_FILE.read_text().strip()))
    except Exception:
        return 0


def _academic_rotation_advance(next_index: int) -> None:
    """Best-effort persist; a read-only filesystem must not kill the batch."""
    try:
        _ACADEMIC_ROTATION_FILE.write_text(str(int(next_index)))
    except Exception:
        pass


# ASSET MODE: prescriptive calendar/session/seasonal concepts per instrument.
# Rotation-based: fires ~1-in-5 non-wild non-macro iterations; _asset_mode_for
# picks ONE concept per visit via hour-bucketed seed so the LLM can't clamp.
#
# CRITICAL: every concept here MUST be implementable using ONLY df['date']
# arithmetic (month, day_of_week, day_of_month, hour_of_day, week-of-year,
# weekend-gap = open - close.shift(1)) plus OHLC columns. If a concept needs
# an event-tag column (CoT positioning, ECB calendar, China data calendar,
# weather data, etc.) the LLM invents that column and the strategy fails
# 0-signal. The phrasing intentionally embeds the date-pattern proxy in
# parentheses so the LLM cuts and pastes it into code.
#
# Empirical motivation: a per-batch audit (forever_20260525_092056.log) showed
# all 3 ASSET slots failed on this exact issue — `cot_report_change`,
# `china_cpi_release`, weekly-LTC. After pruning every concept here is a
# deterministic date pattern.
_ASSET_MODE_CONCEPTS: Dict[str, List[str]] = {
    # FX majors — deterministic date patterns only
    'EUR_USD':   ['NFP Friday window (first Friday of month — day_of_week==4 AND day_of_month<=7)',
                  'Month-end portfolio rebalance flow (last 3 trading days of the month)',
                  'Mid-month US-data cluster (CPI/PPI/retail roughly day_of_month 10-18)'],
    'GBP_USD':   ['Month-end UK fixing flow (last 3 trading days of the month)',
                  'Mid-month UK-data cluster (day_of_month 14-18)',
                  'Weekend re-pricing (Friday-close vs Monday-open gap)'],
    'USD_JPY':   ['BoJ-meeting-window proxy (~day_of_month 5 — month-start vol)',
                  'Month-end Japanese repatriation flow (last 3 trading days)',
                  'Quarter-end JPY-flow (month in 3,6,9,12 AND last week)'],
    'USD_CHF':   ['Month-end repatriation flow (last 3 trading days)',
                  'Tuesday quiet-window mean-reversion (day_of_week==1)'],
    'AUD_USD':   ['RBA first-Tuesday meeting (day_of_week==1 AND day_of_month<=7)',
                  'Mid-month commodity-data window (day_of_month 10-18)',
                  'Friday Asian-data spillover (day_of_week==4)'],
    'NZD_USD':   ['RBNZ-meeting proxy (~6-week cycle, ~first-third Wednesday: day_of_week==2)',
                  'Wellington-Asian-open hour window (hour 21-23 UTC, intraday only)'],
    'EUR_GBP':   ['Month-end ratio rebalance (last 3 trading days)',
                  'Friday afternoon European-close drift (hour 14-16 UTC, intraday only)'],
    'EUR_JPY':   ['Month-end carry-trade rebalance (last 3 trading days)',
                  'Asian-session JPY-flow timing (hour 0-2 UTC, intraday only)'],
    'GBP_JPY':   ['London-session high-vol window (hour 7-10 UTC, intraday only)',
                  'Quarter-end carry rebalance (month in 3,6,9,12 AND last week)'],
    # Metals
    'XAU_USD':   ['NY AM fix hour (hour 13-15 UTC ~8-10am ET, intraday only)',
                  'Month-end ETF rebalance (last 3 trading days)',
                  'NFP-Friday gold reaction (first Friday — day_of_week==4 AND day_of_month<=7)'],
    'XAG_USD':   ['NY AM fix hour (hour 13-15 UTC, intraday only)',
                  'Asian + European industrial-hour (hour 0-12 UTC, intraday only)',
                  'Month-end industrial rebalance (last 3 trading days)'],
    # Energy
    'WTICO_USD': ['Weekly EIA inventory release (day_of_week==2 — Wednesday)',
                  'Driving season seasonal-rise (month in 5,6,7,8)',
                  'Hurricane-season vol regime (month in 6,7,8,9,10,11)',
                  'Weekend re-pricing (Friday-close vs Monday-open gap)'],
    'BCO_USD':   ['European-session vs US-session range (hour 6-12 vs 13-21 UTC, intraday)',
                  'Month-end roll window (last 3 trading days)',
                  'Weekend re-pricing (Fri close vs Mon open)'],
    'NATGAS_USD':['EXTREME winter heating season (month in 11,12,1,2 — seasonal-avg rise)',
                  'Weekly EIA storage release (day_of_week==3 — Thursday)',
                  'Summer cooling-demand window (month in 7,8)',
                  'Hurricane-season Gulf-of-Mexico (month in 6,7,8,9,10,11 — vol regime)'],
    # Grains — USDA WASDE date-pattern + planting/harvest by month
    'CORN_USD':  ['USDA WASDE day-of-month window (day_of_month in 10,11,12,13,14)',
                  'Planting-season vol (month in 4,5)',
                  'Harvest-pressure season (month in 9,10,11)'],
    'SOYBN_USD': ['USDA WASDE day-of-month window (day_of_month in 10,11,12,13,14)',
                  'US harvest season (month in 9,10,11)',
                  'Brazil harvest season (month in 2,3,4 — Southern Hemisphere)'],
    'WHEAT_USD': ['USDA WASDE day-of-month window (day_of_month in 10,11,12,13,14)',
                  'Northern-Hemisphere harvest (month in 6,7,8)',
                  'Winter-wheat planting season (month in 9,10)'],
    # Crypto — 24/7 date/hour-pattern microstructure
    'BTC_USD':   ['Sunday-night Asian-session open (day_of_week==6 AND hour in 22,23 — intraday)',
                  'Weekend (day_of_week in 5,6) vs weekday volatility regime',
                  'Month-end / quarter-end rebalance (last 3 trading days)'],
    'ETH_USD':   ['Weekend (day_of_week in 5,6) vs weekday vol regime',
                  'Month-end rebalance (last 3 trading days)',
                  'Quarter-end rebalance (month in 3,6,9,12 AND last week)'],
    'LTC_USD':   ['Weekend gap (open - close.shift(1) when day_of_week==0 — Monday open)',
                  'Asian-overnight low-liquidity hour window (hour 18-23 UTC, intraday only)',
                  'Month-end rebalance (last 3 trading days)'],
}


def _asset_mode_for(instrument: str, seed: Optional[int] = None) -> Optional[str]:
    """Build a prescriptive ASSET MODE constraint for the instrument — the
    asset-rotation equivalent of _macro_constraint_for.

    Per-visit concept selection: instead of listing all concepts and asking
    the LLM to pick one (which it ignored — primacy bias clamped 7/7 AUD_USD
    visits onto 'RBA first-Tuesday' and 7/7 XAU_USD visits onto 'NY AM fix'),
    pick EXACTLY ONE concept based on a time-bucketed seed. Concept rotates
    every hour, with an instrument-derived offset so different instruments at
    the same hour don't lock-step to the same index.

    The MUST clause + 'plain technical indicators in isolation NOT acceptable'
    clause stay — they're what forced concept compliance in the entry code
    (the genuine win over the soft hint era).

    Returns None for instruments with no concepts defined — callers fall
    through to the creative rotation. `seed` is exposed for deterministic
    tests; the loop passes None and gets hour-bucketed rotation.
    """
    concepts = _ASSET_MODE_CONCEPTS.get(instrument)
    if not concepts:
        return None
    if seed is None:
        # Under a chain A/B the twin batches share a pinned seed, so both arms see an
        # identical rotation and schedule variance cancels out of the comparison.
        seed = (_AB_PAIR_SEED if _AB_PAIR_SEED is not None
                else int(time.time() // 3600))   # else changes every hour
    # Instrument-derived offset prevents lock-step rotation across instruments
    # at the same hour (otherwise every asset slot in a batch picks the same
    # index-mod-N within its own list).
    inst_offset = sum(ord(c) for c in instrument)
    chosen = concepts[(seed + inst_offset) % len(concepts)]
    # Wrapper text lives in categories/asset.md ({instrument}/{chosen} tokens); the
    # per-instrument concept menu (_ASSET_MODE_CONCEPTS) stays in code.
    return _category_constraint('asset', instrument=instrument, chosen=chosen)


# Timeframe forced per iteration. Left free, the thesis model picks 'D' ~93% of
# the time; rotating a forced timeframe ensures intraday strategies actually get
# generated. Starting with H4/H1 only — M30 is deferred (5y of 30-min bars is
# ~60k candles per validation, much slower). M30 is still a VALID timeframe if a
# wild iteration or a manual strategy picks it; it's just not force-rotated yet.
# Daily stays the plurality.
_TIMEFRAME_ROTATION = ['D', 'H4', 'D', 'H1', 'D', 'H4', 'D', 'H1', 'D', 'W']

# Legacy: kept for fallback
DEFAULT_MODEL = THESIS_MODEL
FALLBACK_MODEL = THESIS_FALLBACK

# Max previous failures to include in context (keep small to avoid context overflow)
MAX_FAILURE_CONTEXT = 3

# Fallback prompt if program.md is missing
DEFAULT_PROMPT = """You are a quantitative trading strategy researcher.
Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale.
Code must define generate_signals(df, params) and return pd.Series of int values in {-1,0,1}.
Do not use future data or volume."""

# Output directory for generated candidates
CANDIDATE_DIR = Path(__file__).parent / '.auto-research-candidates'


# ============================================================================
# PROMPT BUILDER
# ============================================================================

def _build_system_prompt() -> str:
    # Load instructions from program.md
    program_path = Path(__file__).parent / 'program.md'
    if program_path.exists():
        with open(program_path) as f:
            return f.read().strip()
    # Fallback to hardcoded prompt
    return DEFAULT_PROMPT


def _get_research_phase() -> str:
    """Extract current research directives from thesis.md (primary) or program.md (fallback)."""
    for path in (Path(__file__).parent / 'thesis.md', Path(__file__).parent / 'program.md'):
        if not path.exists():
            continue
        text = path.read_text()
        start = text.find('<!-- RESEARCH_PHASE_START -->')
        end   = text.find('<!-- RESEARCH_PHASE_END -->')
        if start != -1 and end != -1:
            lines = text[start + len('<!-- RESEARCH_PHASE_START -->'):end].strip()
            return lines if lines else ''
    return ''


_CATEGORY_GUIDANCE_SENTINEL = '<!-- CATEGORY_GUIDANCE'


def _get_thesis_rules() -> str:
    """Load thesis dos/don'ts from thesis.md (cached per process), splicing the
    per-category data guidance in from categories/*.md at the sentinel so each
    category is edited in ONE file. Fails soft: a missing md just contributes ''.
    """
    thesis_path = Path(__file__).parent / 'thesis.md'
    if not thesis_path.exists():
        return ''
    text = thesis_path.read_text()
    # Replace the sentinel line with the assembled macro/pair/event/calendar
    # GUIDANCE blocks (same order/content as the old inline sections).
    guidance = "\n\n".join(
        g for g in (_category_guidance(n)
                    for n in ('macro', 'pair', 'event', 'calendar', 'nnfx', 'academic')) if g
    )
    out_lines = []
    for line in text.split('\n'):
        if line.startswith(_CATEGORY_GUIDANCE_SENTINEL):
            out_lines.append(guidance)
        else:
            out_lines.append(line)
    return "\n".join(out_lines).strip()


_CODEGEN_TEMPLATE_CACHE = None


def _get_codegen_template() -> str:
    """Load the code-generation prompt template from codegen.md.

    The template uses str.format placeholders: {instrument} {timeframe}
    {family} {hypothesis} {entry} {filter} {exit} {param_hints}. The leading
    HTML comment (maintainer notes) is stripped. Raises if the file is
    missing — a broken code-gen prompt must fail loud, not silently.
    """
    global _CODEGEN_TEMPLATE_CACHE
    if _CODEGEN_TEMPLATE_CACHE is not None:
        return _CODEGEN_TEMPLATE_CACHE
    path = Path(__file__).parent / 'codegen.md'
    if not path.exists():
        raise FileNotFoundError(f'codegen.md not found at {path} — cannot build code-gen prompt')
    text = path.read_text()
    # Strip a leading <!-- ... --> maintainer-notes block
    text = re.sub(r'^\s*<!--.*?-->\s*', '', text, count=1, flags=re.DOTALL)
    _CODEGEN_TEMPLATE_CACHE = text.strip()
    return _CODEGEN_TEMPLATE_CACHE
def _shorten(text: str, limit: int = 180) -> str:
    if not text:
        return 'none'
    txt = str(text).strip().replace('\n', ' ')
    return txt if len(txt) <= limit else txt[:limit] + '...'


def _build_user_prompt(
    instrument: str,
    failed_strategies: List[Dict],
    iteration: int
) -> str:
    """Build user prompt with compact failure context to avoid token blowups."""
    lines = [
        f'Generate a new trading strategy for {instrument}.',
        f'This is iteration {iteration}.',
        '',
    ]

    if failed_strategies:
        lines.append('=== PREVIOUSLY FAILED STRATEGIES (DO NOT REPEAT) ===')
        for fs in failed_strategies[:MAX_FAILURE_CONTEXT]:
            fs_status = _shorten(fs.get('final_status', fs.get('status', 'unknown')), 120)
            rationale = _shorten(fs.get('rationale', 'none'), 180)
            lines.append(f'- ID: {fs["id"]} | Status: {fs_status} | Rationale: {rationale}')
            scores = []
            if fs.get('is_gt_score') is not None:
                scores.append(f'IS={fs["is_gt_score"]:.2f}')
            if fs.get('wf_gt_score') is not None:
                scores.append(f'WF={fs["wf_gt_score"]:.2f}')
            if fs.get('ho_gt_score') is not None:
                scores.append(f'HO={fs["ho_gt_score"]:.2f}')
            if scores:
                lines.append(f'  Scores: {", ".join(scores)}')
        lines.append('')
        lines.append('Propose a genuinely DIFFERENT hypothesis. Do NOT tweak parameters of a failed strategy.')
    else:
        lines.append('No prior failures. Propose a fresh, economically-grounded strategy.')

    lines.append('')
    lines.append('Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe.')
    

    return '\n'.join(lines)


# ============================================================================
# LLM CLIENT
# ============================================================================

def _estimate_tokens(text: str) -> int:
    # rough estimate: ~4 chars/token for English/code mix
    return max(1, len(text) // 4)


def _provider_for_model(model: str):
    """OpenRouter `provider` routing override, or None for default routing.

    Pin first-party DeepSeek for its prompt caching: caching only pays off when
    requests consistently land on the SAME provider, and DeepSeek's first-party
    endpoint has long-lived context caching. `allow_fallbacks=True` keeps the
    resilience — it prefers DeepSeek but still routes elsewhere if DeepSeek is
    down (that batch just misses cache). Only PAID first-party models are pinned;
    ':free' deepseek variants route to third-party free hosts, so pinning the
    'deepseek' provider would break them — leave those on default routing.
    """
    if model.startswith('deepseek/') and not model.endswith(':free'):
        return {'order': ['deepseek'], 'allow_fallbacks': True}
    return None


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(
            part.get('text') or part.get('content') or ''
            for part in content
            if isinstance(part, dict)
        )
    return ''


def _chat_content(resp) -> str:
    if resp.text.lstrip().startswith('data:'):
        parts = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            raw = line[5:].strip()
            if raw == '[DONE]':
                break
            try:
                choice = json.loads(raw)['choices'][0]
            except Exception:
                continue
            delta = choice.get('delta') or {}
            msg = choice.get('message') or {}
            parts.append(_message_text(delta.get('content')) or _message_text(msg.get('content')))
        return ''.join(parts)
    data = resp.json()
    if isinstance(data.get('data'), dict) and 'choices' in data['data']:
        data = data['data']
    if 'error' in data and 'choices' not in data:
        raise ValueError(data['error'].get('message', str(data['error'])) if isinstance(data.get('error'), dict) else str(data['error']))
    return _message_text(data['choices'][0]['message'].get('content'))


def call_openrouter(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    api_key: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 60,
) -> Dict[str, Any]:
    """Call one model and feed the outcome to the provider breaker.

    Thin wrapper over _call_openrouter_once so every return path (including the
    exception handlers) is recorded from one place. `model` still carries its
    provider prefix here — _call_openrouter_once strips it.
    """
    res = _call_openrouter_once(system_prompt, user_prompt, model, api_key,
                                temperature, max_tokens, timeout)
    _record_provider_result(model, res.get('success', False), res.get('error'))
    return res


def _call_openrouter_once(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    api_key: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Call OpenRouter API and return parsed JSON response.

    Returns:
        {'success': bool, 'candidate': dict or None, 'error': str or None}
    """
    _reasoning = _reasoning_param(model)   # keyed on the prefixed id, before stripping
    _chain_entry = model                   # _route_model rebinds `model` to the stripped id;
                                           # the unsupported-set is keyed on the prefixed one
    base, key, model, is_direct = _route_model(model, api_key)
    if is_direct and (not base or not key):
        return {'success': False, 'candidate': None,
                'error': 'direct provider endpoint/API key not set in env'}
    if not key:
        return {'success': False, 'candidate': None, 'error': 'OPENROUTER_API_KEY not set'}

    estimated_prompt_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
    print(f'  Prompt size: ~{estimated_prompt_tokens} tokens', flush=True)

    # Guardrail against runaway prompt growth
    if estimated_prompt_tokens > 12000:
        return {
            'success': False,
            'candidate': None,
            'error': f'Prompt too large (~{estimated_prompt_tokens} tokens). Trim failure context.'
        }

    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    if _reasoning is not None:
        payload['reasoning'] = _reasoning     # cap gateway reasoning so answer fits budget
    payload.update(_alibaba_thinking_off(_chain_entry))   # prefixed id — `model` is stripped by now
    if not is_direct:
        _provider = _provider_for_model(model)
        if _provider is not None:
            payload['provider'] = _provider  # OpenRouter-only: pin DeepSeek first-party for caching

    try:
        resp = requests.post(
            f'{base}/chat/completions',
            headers=headers,
            json=payload,
            timeout=timeout
        )
        resp.raise_for_status()
        # A/B PROVENANCE AT THE WIRE. The sidecar records the model this process
        # INTENDED to use; only the response says which one the gateway actually
        # served. A chain that silently fell through to its second entry would
        # otherwise be indistinguishable from the arm working, and the whole
        # experiment would be comparing the control against itself.
        if _AB_STATE:
            try:
                _served = (resp.json().get('data') or resp.json()).get('model')
                if _served and str(_served) != str(model).split(':')[-1]:
                    print(f"  [A/B] served {_served!r} for requested {model!r}", flush=True)
                elif _served:
                    print(f"  [A/B] wire OK — served {_served}", flush=True)
            except Exception:
                pass
        try:
            content = _chat_content(resp)
        except ValueError as e:
            return {'success': False, 'candidate': None, 'error': f'Model error: {str(e)[:200]}'}
        if not content:
            finish = ((resp.json().get('data') or resp.json()).get('choices') or [{}])[0].get('finish_reason')
            return {'success': False, 'candidate': None, 'error': f'Empty content from model (finish_reason={finish})'}

        candidate = _extract_json(content)
        if candidate is None:
            return {'success': False, 'candidate': None, 'error': f'Failed to parse JSON: {content[:200]}'}

        meta = {'route': model, 'base_url': base}
        if isinstance(candidate, dict):
            candidate['_model_meta'] = meta
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    item['_model_meta'] = meta
        return {'success': True, 'candidate': candidate, 'error': None}

    except requests.exceptions.HTTPError as e:
        # A gateway that rejects the `reasoning` field (400) gets one retry
        # without it, so capping never makes an otherwise-good call fail.
        # Fires on ANY 400 while the field is present — gateways that reject it
        # often return an opaque body that never names it (see glm-5.2 note at
        # _REASONING_UNSUPPORTED), so matching on the word missed the real case.
        if resp.status_code == 400 and 'reasoning' in payload:
            payload.pop('reasoning', None)
            _mark_reasoning_unsupported(_chain_entry)
            try:
                resp = requests.post(f'{base}/chat/completions', headers=headers,
                                     json=payload, timeout=timeout)
                resp.raise_for_status()
                content = _chat_content(resp)
                if not content:
                    finish = ((resp.json().get('data') or resp.json()).get('choices') or [{}])[0].get('finish_reason')
                    return {'success': False, 'candidate': None,
                            'error': f'Empty content from model (finish_reason={finish})'}
                candidate = _extract_json(content)
                if candidate is None:
                    return {'success': False, 'candidate': None, 'error': f'Failed to parse JSON: {content[:200]}'}
                meta = {'route': model, 'base_url': base}
                if isinstance(candidate, dict):
                    candidate['_model_meta'] = meta
                elif isinstance(candidate, list):
                    for item in candidate:
                        if isinstance(item, dict):
                            item['_model_meta'] = meta
                return {'success': True, 'candidate': candidate, 'error': None}
            except Exception as re:
                return {'success': False, 'candidate': None, 'error': f'HTTP error after reasoning-strip retry: {re}'}
        try:
            err_body = resp.text[:500]
            return {'success': False, 'candidate': None, 'error': f'HTTP {resp.status_code}: {err_body}'}
        except Exception:
            return {'success': False, 'candidate': None, 'error': f'HTTP error: {e}'}
    except requests.exceptions.Timeout:
        return {'success': False, 'candidate': None, 'error': f'{base} timeout'}
    except requests.exceptions.RequestException as e:
        return {'success': False, 'candidate': None, 'error': f'API error: {e}'}
    except Exception as e:
        # Dump the raw response body so we can diagnose the actual failure
        try:
            raw_body = resp.text[:600]
        except Exception:
            raw_body = '(no response body)'
        return {'success': False, 'candidate': None, 'error': f'Unexpected error: {e} | body: {raw_body}'}


_CODE_SYSTEM_PROMPT = (
    "You are a quantitative trading strategy coder. "
    "Output EXACTLY two fenced blocks and nothing else:\n"
    "1. ```python\\n<generate_signals function>\\n```\n"
    "2. ```json\\n{\"param_grid\": {...}, \"archetype\": \"standard\"}\\n```\n"
    "No explanation, no prose, no extra text."
)


def _extract_code_blocks(text: str) -> Dict[str, Any]:
    """
    Parse the two-block code-gen response format:
      ```python\\n<code>\\n```
      ```json\\n{param_grid, archetype}\\n```

    Returns {'code': str, 'param_grid': dict, 'archetype': str} or raises ValueError.
    """
    import re
    python_code = None
    param_json  = None

    # Extract all fenced blocks
    blocks = re.findall(r'```(\w*)\n(.*?)```', text, re.DOTALL)
    for lang, content in blocks:
        lang = lang.strip().lower()
        content = content.strip()
        if lang == 'python' and python_code is None:
            python_code = content
        elif lang == 'json' and param_json is None:
            param_json = content

    if not python_code:
        raise ValueError('No ```python block found in response')
    if not param_json:
        raise ValueError('No ```json block found in response')

    try:
        meta = json.loads(param_json)
    except json.JSONDecodeError as e:
        raise ValueError(f'param_grid JSON invalid: {e}')

    param_grid = meta.get('param_grid', {})
    if not isinstance(param_grid, dict) or not param_grid:
        raise ValueError('param_grid missing or empty in json block')

    return {
        'code':      python_code,
        'param_grid': param_grid,
        'archetype': meta.get('archetype', 'standard'),
    }


def _is_transient_err(err: str) -> bool:
    e = (err or '').lower()
    return ('429' in e or 'request error' in e or 'timed out' in e
            or 'temporarily' in e or 'resolve' in e or 'connection' in e
            or 'empty content' in e or 'parse error' in e)


def generate_code_via_openrouter(prompt: str, max_retries: int = 2, api_key: str = None) -> Dict[str, Any]:
    """
    Generate strategy code via OpenRouter free models (rotated in order).
    Models return two fenced blocks (python + json) instead of one large JSON
    to avoid truncation on free-tier token limits.

    Retries the whole free list after a short backoff when EVERY model fails on a
    TRANSIENT error (429 / network-DNS blip) — this replaces the dropped paid
    deepseek-chat backstop: a rate-limit that clears in a few seconds becomes a
    success instead of an iteration error. Model-specific failures (parse/empty/4xx)
    do not retry, so a bad generation doesn't waste the backoff.
    Returns {'success': bool, 'candidate': dict or None, 'error': str or None}
    """
    last_error = 'No fallback models configured'
    for backoff in (0, 5, 15):
        if backoff:
            print(f'  [Retry] free models transient-failed — waiting {backoff}s then retrying list', flush=True)
            time.sleep(backoff)
        had_transient = False
        for model in _chain_order(CODE_FALLBACK_MODELS):
            print(f'  [Fallback] Trying {model}...', flush=True)
            _base, _key, _m, _direct = _route_model(model)
            if _direct and (not _base or not _key):
                last_error = 'direct provider endpoint/API key not set in env'
                print(f'  [Fallback] {model} skipped: {last_error}', flush=True)
                continue
            # Raw call (OpenRouter or BytePlus) — we parse the response ourselves
            _codegen_payload = {
                'model': _m,
                'messages': [
                    {'role': 'system', 'content': _CODE_SYSTEM_PROMPT},
                    {'role': 'user',   'content': prompt},
                ],
                'temperature': 0.3,
                # code + small json block + reasoning headroom (the
                # opencode models spend budget on reasoning_content
                # before emitting content; too tight → empty content).
                'max_tokens': 12000,
            }
            _cg_reasoning = _reasoning_param(model)   # cap reasoning; keyed on prefixed id
            if _cg_reasoning is not None:
                _codegen_payload['reasoning'] = _cg_reasoning
            _codegen_payload.update(_alibaba_thinking_off(model))
            try:
                resp = requests.post(
                    f'{_base}/chat/completions',
                    headers={
                        'Authorization': f'Bearer {_key}',
                        'Content-Type': 'application/json',
                    },
                    json=_codegen_payload,
                    timeout=180,
                )
                if resp.status_code == 400 and 'reasoning' in _codegen_payload:
                    _codegen_payload.pop('reasoning', None)   # gateway rejects the field → retry without
                    _mark_reasoning_unsupported(model)
                    resp = requests.post(
                        f'{_base}/chat/completions',
                        headers={'Authorization': f'Bearer {_key}', 'Content-Type': 'application/json'},
                        json=_codegen_payload, timeout=180)
            except requests.exceptions.RequestException as e:
                last_error = f'Request error: {e}'; had_transient = True
                _record_provider_result(model, False, last_error)
                print(f'  [Fallback] {model} request failed: {last_error[:120]}', flush=True)
                continue

            if resp.status_code == 429:
                last_error = f'HTTP 429: {resp.text[:200]}'; had_transient = True
                _record_provider_result(model, False, last_error)
                print(f'  [Fallback] {model} rate-limited/unavailable, trying next...', flush=True)
                continue
            if resp.status_code != 200:
                last_error = f'HTTP {resp.status_code}: {resp.text[:200]}'
                _record_provider_result(model, False, last_error)
                print(f'  [Fallback] {model} failed: {last_error[:120]}', flush=True)
                continue

            try:
                content = _chat_content(resp)
            except ValueError as e:
                last_error = str(e)[:200]
                _record_provider_result(model, False, f'model error: {last_error}')
                print(f'  [Fallback] {model} model error: {last_error[:120]}', flush=True)
                continue

            if not content.strip():
                try:
                    finish = ((resp.json().get('data') or resp.json()).get('choices') or [{}])[0].get('finish_reason')
                except Exception:
                    finish = 'unknown'
                last_error = f'Empty content from model (finish_reason={finish})'
                had_transient = True
                _record_provider_result(model, False, last_error)
                print(f'  [Fallback] {model} failed: {last_error}', flush=True)
                continue

            try:
                blocks = _extract_code_blocks(content)
                blocks['_model_meta'] = {'codegen_route': model, 'codegen_base_url': _base}
                _record_provider_result(model, True)
                print(f'  [Fallback] {model} succeeded', flush=True)
                return {'success': True, 'candidate': blocks, 'error': None}
            except ValueError as e:
                last_error = f'Parse error: {e}'
                had_transient = True
                _record_provider_result(model, False, f'Failed to parse: {e}')
                print(f'  [Fallback] {model} failed: {last_error[:120]}', flush=True)

        if not had_transient:
            break   # all failures were model-specific — a backoff retry won't help

    return {'success': False, 'candidate': None, 'error': f'All fallback models failed. Last: {last_error}'}


# Bounded "exploit" slots (2026-06-16): a few schedule slots steer toward families
# with demonstrated REAL edge that failed ONLY on drawdown, with a risk-control
# instruction. Bounded + NEVER on wild slots, so the random rotation stays the
# exploration backbone — data-driven focus must not collapse diversity.
EXPLOIT_SLOT_EVERY = 15   # ~1 exploit slot per 15 non-wild iterations (~2 of a 31-batch)

# Minimum counted failures for the end-of-batch meta-review to fire. 10, not the
# old 15: the exit-mechanism rule pushed rejections earlier in the funnel, so a
# 20-iteration batch now records 10-12 counted failures where it used to record
# ~19 (the earlier rejections never reach the validation branch that counts).
# Held BELOW the observed floor so a normal batch still refreshes the directive,
# while a batch that mostly SUCCEEDS does not spend a paid LLM call on nothing.
META_REVIEW_MIN_FAILURES = 10
# Whether the bounded exploit slots are active. Default OFF; the A/B controller
# ties it to the data-driven arm, so a 'DRIVEN' batch gets fingerprint + exploit
# and a 'NORMAL' batch gets neither (pure original random rotation).
EXPLOIT_ENABLED = os.environ.get('EXPLOIT_ENABLED', '1') != '0'  # on by default — DRIVEN graduated 2026-06-18


def _exploit_instruments() -> list:
    """DD-blocked-edge families for the bounded exploit slots — or [] when disabled
    (EXPLOIT_ENABLED off → pure rotation) or on any error (fail-soft)."""
    if not EXPLOIT_ENABLED:
        return []
    try:
        from meta_review import dd_blocked_instruments
        return dd_blocked_instruments()
    except Exception:
        return []


# Forced calendar/seasonal slot (2026-06-28): systematically generate two-sided,
# regime-independent flow edges to diversify a book that's otherwise directional beta.
_CALENDAR_CONSTRAINT = _category_constraint('calendar')  # categories/calendar.md

# Forced EVENT slot (2026-07-08). Constraint text lives in categories/event.md; it
# MUST literally contain 'days_to_event'/'event_window' so the schedule's is_event
# daily-pin fires (these columns are day-resolution — meaningless on weekly bars).
_EVENT_CONSTRAINT = _category_constraint('event')  # categories/event.md

# Forced NNFX slot (2026-07-13): multi-layer indicator filter pattern (baseline →
# confirmation → volume → exit) to diversify away from single-indicator entries.
_NNFX_CONSTRAINT = _category_constraint('nnfx')    # categories/nnfx.md


# Every way a batch iteration can end. THE canonical list — `iterations` must
# equal the sum of these, and every one of them must reach the human-facing
# report. Three bugs in one day (2026-08-09) had the same shape: the code was
# right and the surface a human reads was stale — forced slots logging as
# `constraint[N]`, a retired FIX variable still warned about, and this list's
# last two buckets missing from the Telegram report so a 20-iteration batch
# reported as 18. Adding a bucket here without rendering it now FAILS a test
# (tests/test_batch_accounting.py), which is the only thing that makes the class
# of bug self-announcing rather than waiting to be noticed.
BATCH_OUTCOME_KEYS = (
    'passed',                # reached and cleared validation
    'failed',                # built and lost on the merits
    'errors',                # crash or transient API failure — NOTHING else
    'critiqued_out',         # self-critique gate, pre-codegen
    'fingerprint_rejected',  # contradicted the instrument's measured structure
    'guarded',               # deterministic pre-validation skip (bleed, pair-no-instrument2)
)


def batch_counts(results: dict) -> dict:
    """{bucket: int} for every BATCH_OUTCOME_KEYS entry, list-valued or not."""
    out = {}
    for k in BATCH_OUTCOME_KEYS:
        v = results.get(k, 0)
        out[k] = len(v) if isinstance(v, (list, tuple)) else int(v or 0)
    return out


def batch_unaccounted(results: dict) -> int:
    """iterations minus everything rendered. Non-zero means a bucket is missing
    from the accounting (or a legitimate target-reached early stop)."""
    return int(results.get('iterations', 0) or 0) - sum(batch_counts(results).values())


def _slot_label(constraint: str, wild: bool = False) -> str:
    """Name the family a scheduled constraint came from, for the log.

    Derived from the constraint TEXT rather than from the iteration number, so it
    cannot drift out of step with the schedule the way the run loop's own label
    did. Unknown text falls back to CREATIVE — a wrong-but-honest bucket beats a
    confidently wrong index.
    """
    if wild:
        return 'WILD'
    if 'DATA-DRIVEN' in constraint:
        return 'EXPLOIT'
    for name, text in (
            ('ACADEMIC', _category_constraint('academic', anomaly='', instrument='', cols='')),
            ('CALENDAR', _CALENDAR_CONSTRAINT),
            ('EVENT', _EVENT_CONSTRAINT),
            ('NNFX', _NNFX_CONSTRAINT),
    ):
        head = (text or '')[:40]
        if head and constraint.startswith(head):
            return name
    if 'MACRO MODE' in constraint:
        return 'MACRO'
    if constraint in _CREATIVE_CONSTRAINTS:
        return f'CREATIVE[{_CREATIVE_CONSTRAINTS.index(constraint)}]'
    return 'ASSET'


def _build_batch_schedule(instruments: list, max_iterations: int,
                          pool_offset: int = 0, exploit_pool: list = None,
                          steer=None, academic_offset: int = None) -> list:
    """Per-iteration schedule of (inst, constraint, wild, i, detector, tf).

    `academic_offset` is where the academic anomaly walk RESUMES. Pass an int to
    keep this function pure (tests do); leave it None in production and the walk
    is read from, and written back to, the persistent counter — which is the only
    thing stopping the rotation from restarting at 0 every batch and starving the
    tail of the list. See _academic_constraint_for for what that cost.

    Slot priority: wild > exploit > focus > macro > calendar > event > nnfx > asset > creative.
    Wild iterations (every 8th) are the PROTECTED exploration floor — never
    exploit, never focus. Exploit slots are BOUNDED (every EXPLOIT_SLOT_EVERY-th
    non-wild slot) and only fire when an exploit_pool is supplied, so the random
    rotation remains the backbone.

    `steer` is a steering.Steering (hand-edited steering.md). It supplies the
    FOCUS pool, the avoid list and the timeframe rotation. It sits here rather
    than in the prompt because instrument and timeframe are chosen in THIS
    function, before the thesis model is called — a prose directive in thesis.md
    structurally cannot move either (measured 2026-08-03: the live directive said
    "focus on XAU_USD" and XAU_USD came out 2nd-lowest of 31 over 922 gens).
    None → built-in defaults, so every existing caller is unaffected.
    """
    exploit_pool = exploit_pool or []
    if steer is None:
        import steering
        steer = steering.Steering()

    # AVOID is applied to the ROTATION BACKBONE only. Dropping every avoided
    # instrument here (rather than skipping slots later) keeps the schedule
    # exactly max_iterations long — a skip would silently shorten the batch.
    if steer.avoid_instruments:
        kept = [x for x in instruments if x.upper() not in steer.avoid_instruments]
        if kept:
            instruments = kept
        else:
            print('  [steering] avoid_instruments would empty the pool — ignoring it',
                  flush=True)

    focus_pool = [x for x in steer.focus_instruments
                  if x not in steer.avoid_instruments]
    tf_rotation = steer.timeframe_rotation or _TIMEFRAME_ROTATION

    schedule = []
    n_exploit = 0
    n_focus = 0
    # None => production: resume the persistent walk and write it back below.
    _academic_persist = academic_offset is None
    n_academic = _academic_rotation_offset() if _academic_persist else academic_offset
    n_creative = 0
    for i in range(1, max_iterations + 1):
        inst = instruments[(i - 1 + pool_offset) % len(instruments)]
        wild = (i % 8 == 0)
        exploit = (not wild) and bool(exploit_pool) and (i % EXPLOIT_SLOT_EVERY == 0)
        # FOCUS: bounded over-sampling of hand-picked instruments. Ranks BELOW
        # exploit so it can never cannibalise the DD-blocked slots, and below
        # wild so the exploration floor is untouched. It overrides only the
        # INSTRUMENT — the slot keeps whatever constraint it would have had, so
        # a focused instrument still gets the full variety of mechanisms rather
        # than being pinned to one flavour.
        focus = (not wild) and (not exploit) and bool(focus_pool) \
            and (i % steer.focus_slot_every == 0)
        if focus:
            inst = focus_pool[n_focus % len(focus_pool)]
            n_focus += 1
        macro = (not wild) and (not exploit) and (i % 3 == 0)
        # Calendar/seasonal forcing DIALED BACK 2026-07-06 (i%4 -> i%10): a
        # 13-day gen audit showed calendar was 13% of all theses for ~0 durable
        # yield — worst HO of any family (HO_reached 0.12-0.22) and 1 pass in
        # ~1900 gens (day-of-week/turn-of-month seasonals rarely survive holdout;
        # see gbpjpy i13, Sharpe halved dev->HO). Kept a SMALL presence (~5%) for
        # diversity, not zero, so gen doesn't collapse back to the price-only
        # mean-reversion monoculture. Freed slots fall through to free/price-only.
        calendar = (not wild) and (not exploit) and (not macro) and (i % 10 == 0)
        # Forced EVENT slot (2026-07-08), ~5% — mirrors calendar. Offset i%10==5 so
        # it never collides with calendar's i%10==0. Event used to be 1-of-11
        # creative constraints the model ignored ~90% of the time (batch scheduled
        # ~97 slots → ~9 real event strategies); a dedicated slot with the strong
        # _EVENT_CONSTRAINT gives the family a fair, measured test.
        event = (not wild) and (not exploit) and (not macro) and (not calendar) and (i % 10 == 5)
        # Forced NNFX slot (2026-07-13), reduced 2026-07-22 (~7% -> ~2%):
        # 1/1102 pass rate and many timeouts. Keep a tiny diversity tail only.
        nnfx = (not wild) and (not exploit) and (not macro) and (not calendar) and (not event) and (i % 40 == 7)
        asset_constraint = None
        if not wild and not exploit and not macro and not calendar and not event and not nnfx and (i % 9 == 0):
            asset_constraint = _asset_mode_for(inst)
        asset = asset_constraint is not None
        # Forced ACADEMIC-RECALL slot (2026-08-09), ~15%. Ranked LAST so it draws
        # only from the free creative backbone and cannibalises no existing family.
        # i%6==1 implies i%3==1, so it can never contend with the macro slot; the
        # residue is odd, so it never lands on calendar's i%10==0 either.
        # Started deliberately small: nnfx was forced at ~7% for 1 pass in 1102
        # gens and calendar reached 13% of theses for ~0 durable passes, so a new
        # family earns its share from measured pass rate, not from the pitch.
        academic = (not wild) and (not exploit) and (not macro) and (not calendar) \
            and (not event) and (not nnfx) and (not asset) and (i % 6 == 1)
        if wild:
            constraint = _category_constraint('wild')   # categories/wild.md
            detector = None
        elif exploit:
            inst = exploit_pool[n_exploit % len(exploit_pool)]
            n_exploit += 1
            constraint = (
                "DATA-DRIVEN: this instrument has shown REAL, permutation-validated "
                "edge that BLEW the drawdown limit. Design an edge for it with "
                "built-in drawdown control — regime gating, tighter ATR stops, "
                "smaller position size, shorter holds."
            )
            detector = _REGIME_DETECTORS[i % len(_REGIME_DETECTORS)]
        elif macro:
            constraint = _macro_constraint_for(inst)
            detector = _REGIME_DETECTORS[i % len(_REGIME_DETECTORS)]
        elif calendar:
            constraint = _CALENDAR_CONSTRAINT
            detector = None    # the calendar window IS the regime gate
        elif event:
            constraint = _EVENT_CONSTRAINT
            detector = None    # the event window IS the regime gate
        elif nnfx:
            constraint = _NNFX_CONSTRAINT
            detector = None    # the multi-layer filter IS the regime gate
        elif asset:
            constraint = asset_constraint
            detector = _REGIME_DETECTORS[i % len(_REGIME_DETECTORS)]
        elif academic:
            constraint = _academic_constraint_for(inst, n_academic, tf_rotation)
            n_academic += 1
            detector = None    # the anomaly's documented regime dependency IS the gate
        else:
            # Counter, not iteration index. Indexed by i, TWO of the ten creative
            # constraints were unreachable: calendar owns i%10==0 and event owns
            # i%10==5 and both outrank this branch, so `Must avoid all moving-
            # average crossover logic...` and `Entry only on breakout above/below
            # a quantile...` could never be scheduled at all (dead since the event
            # slot landed 2026-07-08; measured over 3000 iterations).
            creative_n = n_creative
            constraint = _CREATIVE_CONSTRAINTS[creative_n % len(_CREATIVE_CONSTRAINTS)]
            detector = _REGIME_DETECTORS[i % len(_REGIME_DETECTORS)]
            n_creative += 1
        # The event-timing constraint lives in _CREATIVE_CONSTRAINTS, so it used
        # to inherit the weekly-inclusive rotation below — fatal: days_to_event /
        # event_window are DAY-resolution, and on weekly bars ~48% of weeks
        # contain an event (zero selectivity) → near-zero coherent signals → the
        # whole family failed at IS=0 (191 gens / 0 passes, 72% were weekly).
        # Pin it to daily (event_window is 14% selective there). 2026-07-06.
        is_event = 'days_to_event' in constraint or 'event_window' in constraint
        # Day-of-week / session effects are DAY-resolution for the same reason the
        # event columns are. This constraint used to be welded to H4 by the index
        # aliasing; decoupling the timeframe freed it to reach WEEKLY, where
        # "day-of-week" is not addressable at all — so pin it, rather than trade
        # one silent defect for another.
        is_day_of_week = 'day-of-week' in constraint or 'time-of-session' in constraint
        # ACADEMIC timeframes are pinned by anomaly, not rotated. Every anomaly in
        # the rotation is documented at daily-or-slower horizons: a 12-1 momentum
        # lookback on H1 bars spans more history than the fetch window holds, and
        # turn-of-month is not addressable on weekly bars. Long-term reversal is
        # the one that needs WEEKLY — a 3-5 year lookback on daily bars consumes
        # most of the sample before the first signal. (The i%6==1 residue happened
        # to alias onto all-'D' anyway; this makes it a decision, so it survives
        # an edit to the residue or to _TIMEFRAME_ROTATION.)
        academic_tf = None
        if academic:
            academic_tf = 'W' if 'Long-Term Reversal' in constraint else 'D'
        if wild:
            tf = None
        elif academic:
            tf = academic_tf
        elif exploit or asset or calendar or is_event or is_day_of_week:
            tf = 'D'    # calendar/event effects are day-resolution — never weekly
        elif nnfx:
            # Explicitly daily. This read as `tf_rotation[(i-1) % len]` but every
            # nnfx i (i%40==7) gives the SAME index, so it was constant 'D' while
            # looking rotated. Daily is the right answer for a multi-layer
            # indicator filter, so the behaviour is unchanged — only now it says so.
            tf = 'D'
        else:
            # The creative constraint index and the timeframe index both ran off
            # i%10, locked one apart, so each constraint was welded to exactly ONE
            # timeframe forever (measured over 3000 iterations): "day-of-week or
            # time-of-session effects" only ever ran on H4, and WEEKLY never
            # reached a creative slot at all, because rotation index 9 paired with
            # the constraint calendar had already claimed.
            #
            # Walking the constraint counter and advancing the timeframe by one
            # extra step per completed constraint cycle decouples them: over
            # len(CC) * len(tf_rotation) creative slots every pairing occurs.
            tf = tf_rotation[(creative_n + creative_n // len(_CREATIVE_CONSTRAINTS))
                             % len(tf_rotation)]
        schedule.append((inst, constraint, wild, i, detector, tf))
    # Advance by however many slots this batch actually consumed — never by a
    # fixed stride. A stride multiplied against the list length is exactly the
    # residue aliasing that made half the list unreachable the first time.
    if _academic_persist:
        _academic_rotation_advance(n_academic)
    return schedule


def _generate_thesis_batch(
    instruments: list,
    max_iterations: int,
    failed_ctx: str = "",
    phase_block: str = "",
    pool_offset: int = 0,
) -> list:
    """
    Generate all thesis objects for one batch via OpenRouter.

    Returns a list of dicts (one per iteration), in the same order as the
    instruments × constraint schedule.  Each dict has the same keys as the
    single-thesis JSON format plus "instrument".

    Falls back to an empty list on any error — callers then generate theses
    individually as before.
    """
    # Random-rotation backbone + bounded data-driven exploit slots (see
    # _build_batch_schedule). exploit_pool is fail-soft: [] -> pure rotation.
    # steering.md is re-read HERE, once per batch, so a hand edit takes effect on
    # the next batch without restarting the forever loop.
    import steering
    _steer = steering.load()
    if _steer.focus_instruments or _steer.avoid_instruments:
        print(f'  [steering] focus={_steer.focus_instruments or "-"} '
              f'(1 slot per {_steer.focus_slot_every}) '
              f'avoid={_steer.avoid_instruments or "-"}', flush=True)
    schedule = _build_batch_schedule(instruments, max_iterations, pool_offset,
                                     exploit_pool=_exploit_instruments(),
                                     steer=_steer)

    # Format items list for the prompt. With fingerprinting on, each line carries
    # the instrument's measured in-sample structure so the model designs FOR it.
    # Rendered per sub-batch chunk (local 1-based numbering within each call).
    def _render_items(sched_slice):
        return "\n".join(
            f'{idx}. Instrument={inst} | {"[WILD] " if wild else ""}CONSTRAINT: {constraint}'
            + (f' | TIMEFRAME: {tf} (design ALL conditions for {tf} bars)' if tf else '')
            + (f' | REGIME DETECTOR (filter_condition MUST use this detector, as an INDEPENDENT'
               f' gate — the entry_condition must NOT be built from {detector}): {detector}'
               if detector else '')
            + (lambda s: f'\n   {s}' if s else '')(_fp_compact(inst, tf))
            for idx, (inst, constraint, wild, _, detector, tf) in enumerate(sched_slice, 1)
        )

    _thesis_rules = _get_thesis_rules()
    # Prompt caching is prefix-based: providers cache only the byte-identical
    # leading span of a request. So ALL static content goes in the SYSTEM prompt
    # (the stable prefix) and only per-batch content (the rotating ITEMS schedule,
    # failed-strategy context, research directives) goes in the user message below,
    # ordered stable→variable so the cached prefix runs as deep as possible.
    # (2026-06-16: moved the static "Rules for ALL theses" + output-format blocks
    # here from the TAIL of the user message, where they sat after the variable
    # blocks and therefore never cached.)
    batch_system = (
        "You are a quantitative trading researcher. "
        "Output ONLY valid JSON — a single top-level array. No explanation, no markdown.\n\n"
        + _thesis_rules
        + "\n\nRules for ALL theses:\n"
        "- ALL conditions must use the SAME single timeframe (D, H4, H1, or M30)\n"
        "- Do NOT mix timeframes within one strategy\n"
        "- Express higher-TF context as longer rolling windows\n"
        "- Each strategy must be mechanically different from the others\n"
        "- Where an item specifies a REGIME DETECTOR, the filter_condition MUST be built\n"
        "  from that exact detector — do NOT substitute ADX or another detector. The\n"
        "  detector is an INDEPENDENT regime gate: the entry_condition must trigger on a\n"
        "  DIFFERENT signal, NOT restate the detector. e.g. detector=turn_of_month → the\n"
        "  filter gates BY turn-of-month but the entry fires on a price/vol signal, not on\n"
        "  turn_of_month itself. A filter that repeats the entry's own condition is REJECTED.\n"
        "- Where an item specifies a TIMEFRAME, set the thesis 'timeframe' to it and design\n"
        "  every lookback/window for that bar size — do NOT default to daily\n"
        "- Where an item carries a STRUCTURE[...] block, those are the instrument's MEASURED\n"
        "  in-sample statistics (return autocorrelation ac1/ac5, efficiency ratio ER, vol\n"
        "  clustering, skew, calendar). DESIGN THE MECHANISM TO EXPLOIT THAT MEASURED\n"
        "  STRUCTURE; do NOT assume behaviour the data contradicts — e.g. no mean-reversion\n"
        "  when ac1>0, no trend-following on a choppy low-ER series. Lean on what IS present\n"
        "  (persistent vol regimes, fat tails, a real calendar effect).\n"
        "- The exit_condition MUST reference the SAME quantity the entry is built on, so that\n"
        "  the exit expresses the thesis ENDING rather than merely elapsing. If the entry\n"
        "  fires on a yield spread, the exit reads that spread; on a channel break, it reads\n"
        "  the channel; on a z-score, it reads the z-score. A BARE BAR COUNT ('exit after N\n"
        "  bars') as the ONLY exit is REJECTED — a horizon unrelated to the mechanism cannot\n"
        "  express why the edge stops. A bar count is allowed ONLY as a secondary timeout\n"
        "  beside a mechanism exit ('exit when the spread crosses back below its mean, or\n"
        "  after 10 bars, whichever comes first'). An ATR or volatility stop DOES satisfy\n"
        "  this rule — it reads the market, and the backtest models such a stop for every\n"
        "  strategy regardless. Only a BARE HORIZON with nothing else fails it.\n"
        "- strategy_family must name the mechanism the ENTRY actually uses, not the filter\n"
        "  bolted on to it. A Donchian/high-low channel break is 'regime' whatever gates it;\n"
        "  labelling it 'speed-based' because a session filter is attached, or 'flow-proxy'\n"
        "  because an efficiency-ratio gate is attached, is REJECTED. Definitions —\n"
        "    speed-based: edge from the RATE of change or time-of-day/session microstructure\n"
        "    cross-market: edge from a SECOND instrument (spread, ratio, lead-lag); needs instrument2\n"
        "    regime:       edge from trend/range state — breakouts, channels, MA structure\n"
        "    flow-proxy:   edge from an observable flow footprint — gaps, range extension, volume\n"
        "    event-driven: edge anchored to a scheduled EVENT or calendar date\n"
        "    statistical:  edge from a distributional property — z-score, RSI extreme, dispersion\n"
        "    risk-factor:  edge from a MACRO driver — rates, real yields, the dollar, carry\n"
        "- Across the ITEMS in THIS batch, vary the ENTRY primitive. Do not return a\n"
        "  channel/Donchian/highest-high breakout for more than a QUARTER of the items;\n"
        "  a family label does not make two breakouts different strategies.\n\n"
        "OUTPUT FORMAT — reply with ONLY a JSON array, one object per line-item in "
        "the ITEMS list, in the SAME order. Each object has this exact shape:\n"
        "[\n"
        '  {"instrument":"EUR_USD","strategy_family":"regime","timeframe":"D",'
        '"rationale":"One sentence WHY.","entry_condition":"Exact measurable entry.",'
        '"filter_condition":"Regime/vol filter with exact thresholds.",'
        '"exit_condition":"Exit that READS THE ENTRY\'S OWN quantity (indicator cross, '
        'level reclaimed, spread reverted); a bare bar count alone is REJECTED.",'
        '"param_hints":{"lookback":[10,20,30],"threshold":[0.5,1.0]}},\n'
        "  ...\n"
        "]\n\n"
        "CROSS-MARKET / PAIR — the instrument2 FIELD is MANDATORY (not prose). If a "
        "condition uses close_leg2 or a spread/ratio/divergence between two markets, "
        'you MUST add an "instrument2" key naming the SECOND leg as a valid OANDA '
        "symbol (XAG_USD, CORN_USD, EUR_USD — INSTRUMENT_UNDERSCORE format, a REAL "
        "tradeable instrument, NEVER a ratio like ETH_BTC or a bare name like GOLD). "
        "Naming it only inside entry_condition text is NOT enough — the loader reads "
        "the instrument2 KEY; without it the strategy is DISCARDED. Copy this exact "
        "shape for a pair thesis:\n"
        '  {"instrument":"XAU_USD","instrument2":"XAG_USD","strategy_family":"cross-market",'
        '"timeframe":"D","rationale":"Gold/silver ratio mean-reverts when correlation is high.",'
        '"entry_condition":"go LONG when z-score of (close/close_leg2) over 60 bars < -2; '
        'go SHORT when > +2","filter_condition":"realized vol below its 60-bar median",'
        '"exit_condition":"exit when z-score crosses 0",'
        '"param_hints":{"lookback":[40,60,80],"z_thresh":[1.5,2.0]}}\n'
        "Omit instrument2 entirely for single-instrument strategies."
    )

    # ---- SUB-BATCHED generation (2026-07-02) ----
    # One 31-thesis call caused CROSS-ITEM CONTENT BLEED: the model stamps the
    # correct instrument on an item while writing ANOTHER item's rationale
    # ("rationale about WTI, trading NATGAS") — ~16% of self-critique rejects,
    # invisible to the declared-instrument guard below because the field is
    # right and only the narrative is wrong. Smaller calls = less context to
    # bleed across, and shorter outputs also kill the truncation/timeout
    # failure mode outright. The system prompt is byte-identical for every
    # chunk so the provider prefix-cache still applies; cost is ~3 extra small
    # calls per batch. A failed chunk None-fills only its own slots (per-iter
    # fallback regenerates those) instead of dumping the WHOLE batch.
    THESIS_CHUNK = 8
    THESIS_HTTP_TIMEOUT = 300   # generous; ~8 theses ≈ 3.6k output tokens ≈ 40s on Flash

    def _cascade(chunk_prompt, n_items):
        """Try primary → fallback → paid-final, with one outer network retry."""
        res = {'success': False, 'error': 'no attempt made'}
        for outer_attempt in range(2):
            if outer_attempt > 0:
                print("  [Batch thesis] Outer retry after 10s backoff (network resilience)...", flush=True)
                time.sleep(10)
            for mdl in _chain_order(THESIS_MODELS):
                res = call_openrouter(
                    system_prompt=batch_system,
                    user_prompt=chunk_prompt,
                    model=mdl,
                    api_key=None,
                    temperature=0.7,
                    # 1800/item, floor 24000 — the answer itself is only ~2.3k
                    # completion tokens for an 8-item chunk, so this is entirely
                    # reasoning headroom. Measured 2026-07-23 on an 8-item chunk:
                    # deepseek-v4-pro and -flash both return empty content at
                    # 4000, both return 8/8 at 12000. Raised 2026-07-24: 12000
                    # sat right at the edge — deepseek-v4-pro (the chain lead)
                    # still burned the whole budget on reasoning and returned
                    # finish_reason=length with content='' on roughly a third of
                    # live sub-batches, costing a ~60s dead call each time.
                    # max_tokens is a ceiling, not a reservation, so the headroom
                    # is free on calls that don't need it.
                    max_tokens=max(24000, n_items * 1800),
                    timeout=THESIS_HTTP_TIMEOUT,
                )
                if res['success']:
                    return res
                print(f"  [Batch thesis] {mdl} failed: {res['error'][:120]}", flush=True)
        return res

    n_chunks = (len(schedule) + THESIS_CHUNK - 1) // THESIS_CHUNK
    print(f"  [Batch thesis] Generating {max_iterations} theses in {n_chunks} sub-batches of ≤{THESIS_CHUNK}...", flush=True)

    result = []
    bad_count = 0
    for c0 in range(0, len(schedule), THESIS_CHUNK):
        chunk = schedule[c0:c0 + THESIS_CHUNK]
        # Per-chunk user message, ordered stable→variable for prompt caching.
        chunk_prompt = (
            f"Generate exactly {len(chunk)} trading strategy theses, one per "
            f"line-item in the ITEMS list below. Each MUST follow its specific "
            f"CONSTRAINT, and the output array MUST contain exactly {len(chunk)} "
            f"objects in the same order as the items.\n"
            f"{phase_block}"
            f"{failed_ctx}"
            f"\nITEMS:\n{_render_items(chunk)}\n"
        )
        res = _cascade(chunk_prompt, len(chunk))
        raw = res['candidate'] if res['success'] else None
        if not isinstance(raw, list):
            if res['success']:
                print(f"  [Batch thesis] chunk {c0//THESIS_CHUNK+1}: expected array, got {type(raw).__name__} — slots will regenerate", flush=True)
            else:
                print(f"  [Batch thesis] chunk {c0//THESIS_CHUNK+1}: all models failed — slots will regenerate", flush=True)
            result.extend([None] * len(chunk))
            bad_count += len(chunk)
            continue

        for j, slot in enumerate(chunk):
            gidx = c0 + j          # global slot number (for logs)
            item = raw[j] if j < len(raw) else None
            if not isinstance(item, dict):
                result.append(None)
                bad_count += 1
                continue
            # Misalignment guard: if the model's declared instrument disagrees
            # with the slot, the array is shifted — stamping the schedule
            # instrument would attach this rationale to the WRONG instrument.
            # Drop it; the per-iteration fallback regenerates it. (Content-level
            # bleed with a CORRECT declared field is what the sub-batching
            # above addresses; self-critique remains the backstop.)
            declared = re.sub(r'[^A-Z0-9]', '', str(item.get('instrument', '')).upper())
            expected = re.sub(r'[^A-Z0-9]', '', slot[0].upper())
            if declared and declared != expected:
                print(f"  [Batch thesis] item {gidx+1} instrument mismatch "
                      f"(model wrote {item.get('instrument')!r}, slot is {slot[0]}) "
                      f"— will regenerate", flush=True)
                result.append(None)
                bad_count += 1
                continue
            item['instrument'] = slot[0]
            # Content-bleed guard: the field is correct but the rationale may
            # narrate a DIFFERENT instrument (model reached for a canonical
            # exemplar). Drop pre-critique instead of spending a critique call.
            # EXEMPT cross-market/pair theses — they legitimately name a second
            # instrument (e.g. a BTC slot discussing ETH divergence).
            _fam = str(item.get('strategy_family', '')).lower()
            _cross = ('cross' in _fam or 'pair' in _fam or item.get('instrument2'))
            bleed_kw = None if _cross else _rationale_instrument_mismatch(
                item.get('rationale', ''), slot[0])
            if bleed_kw:
                print(f"  [Batch thesis] item {gidx+1} rationale bleed "
                      f"(mentions {bleed_kw!r} but slot is {slot[0]}) — will regenerate", flush=True)
                result.append(None)
                bad_count += 1
                continue
            # Stamp the slot that actually produced this thesis. The run loop
            # recomputes its own label from the iteration number, which is right
            # only for the per-iteration fallback path — when the thesis comes
            # from the BATCH, that label describes a slot that never ran. Every
            # forced slot was logging as a creative constraint (an academic
            # 12-1 momentum thesis printed as "[constraint[1]]"), so the log could
            # not be used to attribute a failure to a family. Observed 2026-08-09.
            item['_slot_label'] = _slot_label(slot[1], slot[2])
            sched_tf = slot[5]
            if sched_tf:
                # Forced timeframe — stamp it so a forced intraday slot can't be
                # silently overridden back to 'D' by the model.
                item['timeframe'] = sched_tf
            elif 'timeframe' in item:
                item['timeframe'] = item['timeframe'].strip().upper()
            # Validate — mark as None if invalid so the loop falls back per-iteration
            err = _validate_thesis(item)
            if err:
                print(f"  [Batch thesis] item {gidx+1} invalid ({err}) — will regenerate", flush=True)
                result.append(None)
                bad_count += 1
                continue
            result.append(item)

    # Pad with None if the schedule was shorter than requested
    while len(result) < max_iterations:
        result.append(None)
        bad_count += 1

    ok_count = max_iterations - bad_count
    print(f"  [Batch thesis] ✓ {ok_count}/{max_iterations} theses valid", flush=True)
    return result




def _validate_code(code: str) -> tuple:
    """Validate strategy code before execution. Returns (error_str_or_None, cleaned_code)."""
    if not code or 'generate_signals' not in code:
        return ('missing generate_signals function', code)

    code_clean = code

    # Fix uppercase AND/OR/NOT (Python uses lowercase)
    code_clean = re.sub(r'\bAND\b', 'and', code_clean)
    code_clean = re.sub(r'\bOR\b', 'or', code_clean)
    code_clean = re.sub(r'\bNOT\b', 'not', code_clean)

    # Auto-repair pass 1: (expr) and (expr) patterns — loop until convergence
    # Handles chained: (A) and (B) and (C) → one pass each cycle
    for _ in range(15):
        prev = code_clean
        code_clean = re.sub(
            r'\(([^()]+)\)\s+and\s+\(([^()]+)\)',
            lambda m: f'({m.group(1)}) & ({m.group(2)})',
            code_clean
        )
        code_clean = re.sub(
            r'\(([^()]+)\)\s+or\s+\(([^()]+)\)',
            lambda m: f'({m.group(1)}) | ({m.group(2)})',
            code_clean
        )
        if code_clean == prev:
            break

    # Auto-repair pass 2: bare Series boolean assignments
    # Target lines like: long_signal = long_entry and uptrend and vol_ok
    # Must NOT touch: scalar if conditions with .iloc, plain Python logic, comments/strings
    repaired_lines = []
    for ln in code_clean.split('\n'):
        if ln.strip().startswith('#'):
            repaired_lines.append(ln)
            continue
        # Skip scalar loop contexts (if/elif/while with .iloc — these are definitely scalars)
        if re.match(r'\s*(?:if|elif|while)\s+.*\.iloc\[', ln):
            repaired_lines.append(ln)
            continue
        # Series indicator pattern — used for both assignment and if/elif lines
        _series_pat = (r'df\[|\.rolling\b|\.shift\b|\.ewm\b|_entry\b|_filter\b|_signal\b|'
                       r'\btrend\b|_break\b|_cross\b|long_|short_|uptrend|downtrend')
        if re.search(r'\band\b|\bor\b', ln):
            # Repair assignment lines (not if/elif) — original behaviour
            is_assignment = '=' in ln and not ln.strip().startswith(('if ', 'elif ', 'while '))
            # Also repair if/elif lines that clearly reference Series objects
            is_if_series = (re.match(r'\s*(?:if|elif)\b', ln)
                            and re.search(_series_pat, ln)
                            and not re.search(r'\.iloc\[', ln))
            if (is_assignment or is_if_series) and re.search(_series_pat, ln):
                ln = re.sub(r'\band\b', '&', ln)
                ln = re.sub(r'\bor\b', '|', ln)
        repaired_lines.append(ln)
    code_clean = '\n'.join(repaired_lines)

    # After auto-repair, reject ANY remaining and/or in assignment/boolean contexts
    # These patterns indicate Series boolean misuse that auto-repair didn't catch
    # Match: "series_expr and series_expr" without parentheses on BOTH sides
    # Exclude: "if bool(...)" and "if ...:" (scalar contexts), ".iloc[i]" (scalar access)
    lines = code_clean.split('\n')
    for i, line in enumerate(lines, 1):
        # Skip comment lines
        if line.strip().startswith('#'):
            continue
        # Skip if/elif/while scalar contexts (loop body with .iloc access — those are scalars, fine)
        if re.match(r'\s*(?:if|elif|while)\s+bool\(', line):
            continue
        if re.match(r'\s*(?:if|elif|while)\s+.*\.iloc\[', line):
            continue
        # Detect and/or ONLY when the line clearly references pandas Series objects.
        # Scalar variables inside loops (e.g. s = arr[i]; result = (not np.isnan(s)) and (s > 0))
        # are valid Python and should NOT be flagged.
        if re.search(r'\band\b|\bor\b', line):
            is_series_context = bool(re.search(
                r'df\[|\.rolling\b|\.shift\b|\.ewm\b|\.cumsum\b|\.pct_change\b|'
                r'\blong_entry\b|\bshort_entry\b|\buptrend\b|\bdowntrend\b|'
                r'\b\w+_entry\s*[=&|]|\b\w+_filter\s*[=&|]|\b\w+_signal\s*[=&|]|'
                r'\b\w+_break\s*[=&|]|\b\w+_cross\s*[=&|]',
                line
            ))
            if is_series_context:
                return (f'line {i}: uses Python "and"/"or" between expressions (use "&" and "|" with parentheses)', code)

    # Also reject mixed bitwise + logical operators without explicit parens
    if re.search(r'&\s*(and|or)|(and|or)\s*&', code_clean):
        return ('mixed "&" and "and"/"or" without parentheses (precedence ambiguous; wrap in parens)', code)

    try:
        import ast
        ast.parse(code_clean)
    except SyntaxError as e:
        return (f'Invalid Python syntax: {e}', code)

    if 'shift(-1)' in code_clean:
        return ('uses look-ahead bias (shift(-1))', code)

    if 'df["volume"]' in code_clean or "df['volume']" in code_clean or 'df.volume' in code_clean:
        return ('references df volume column (does not exist in OHLC data)', code)
    if "'Volume'" in code_clean or '"Volume"' in code_clean:
        return ('references Volume column', code)

    # Detect references to non-OHLC columns (macro data that doesn't exist in the feed).
    # Rule: any df['col'] read where col is not in the valid set AND never written to in-code.
    from macro_fetcher import ALL_MACRO_COLS
    from supplementary_data import CALENDAR_COLS
    _VALID_DF_COLS = frozenset({
        'close', 'open', 'high', 'low', 'date',            # standard OHLC
        'spread',                                           # spread archetype
        'days_to_event', 'days_since_event', 'event_window',  # news (event-timing) archetype
        'session',                                          # session archetype
        'close_leg2',                                       # pair archetype
    }) | ALL_MACRO_COLS | CALENDAR_COLS                    # macro + calendar archetypes
    all_refs  = set(re.findall(r'df\[["\'](\w+)["\']\]', code_clean))
    write_refs = set(re.findall(r'df\[["\'](\w+)["\']\]\s*=', code_clean))
    external_reads = all_refs - write_refs
    bad_cols = external_reads - _VALID_DF_COLS
    if bad_cols:
        return (
            f'references non-OHLC columns not available in dataframe: {sorted(bad_cols)}',
            code
        )
    if 'import talib' in code_clean:
        return ('uses talib instead of ta library', code)
    # Auto-inject missing standard imports instead of hard-failing
    if 'import pandas' not in code_clean and 'import pd' not in code_clean:
        code_clean = 'import pandas as pd\n' + code_clean
    has_ta = 'import ta' in code_clean or 'from ta' in code_clean
    has_np = 'import numpy' in code_clean or 'import np' in code_clean
    if not has_ta and not has_np:
        code_clean = 'import numpy as np\n' + code_clean
    _price_refs = ('df.low', 'df.high', 'df.close', 'df.open',
                   'df["close"]', "df['close']", 'df["high"]', "df['high']",
                   'df["low"]', "df['low']", 'df["open"]', "df['open']",
                   'df["Close"]', 'df["High"]', 'df["Low"]', 'df["Open"]')
    if not any(ref in code_clean for ref in _price_refs):
        return ('never references price data (close/high/low)', code)

    if 'ta.momentum.cci' in code_clean:
        return ('use ta.trend.cci NOT ta.momentum.cci', code)
    if 'ta.trend.aroon[' in code_clean or 'ta.trend.aroon(' in code_clean:
        return ('use ta.trend.aroon_up() and ta.trend.aroon_down() (returns Series)', code)
    if 'ta.volatility.supertrend' in code_clean:
        return ('use ta.trend.supertrendindicator from ta.trend', code)
    if 'ta.trend.williams' in code_clean:
        return ('use ta.momentum.williams_r', code)

    return (None, code_clean)


def _infer_archetype(code: str, declared: str = 'standard') -> str:
    """Derive the archetype from the columns the code actually references.

    The code-gen LLM self-reports `archetype` in its JSON, but does so
    unreliably — a macro strategy regularly comes back tagged 'standard', so
    inject_supplementary_data skips the macro columns and the code KeyErrors
    on us10y / fed_rate / etc. The code is the source of truth; the declared
    value is only a fallback when the code references plain OHLC.
    """
    from macro_fetcher import ALL_MACRO_COLS
    from supplementary_data import CALENDAR_COLS
    refs = set(re.findall(r'df\[["\'](\w+)["\']\]', code or ''))
    if refs & ALL_MACRO_COLS:
        return 'macro'
    if refs & CALENDAR_COLS:
        return 'calendar'
    if 'session' in refs:
        return 'session'
    if refs & {'days_to_event', 'days_since_event', 'event_window'}:
        return 'news'
    if 'close_leg2' in refs:
        return 'pair'
    if 'spread' in refs:
        return 'spread'
    return declared or 'standard'


def _validate_param_grid_shape(param_grid: dict) -> Optional[str]:
    if not isinstance(param_grid, dict) or not param_grid:
        return 'param_grid missing or empty'
    combos = 1
    for values in param_grid.values():
        combos *= len(values) if isinstance(values, list) else 1
    if len(param_grid) > 4:
        return f'param_grid has {len(param_grid)} params (max 4)'
    if combos > 200:
        return f'param_grid has {combos} combinations (max 200)'
    return None


def _validate_basic_signals(code: str, param_grid: dict, min_signals: int = 5,
                            instrument: str = 'EUR_USD', timeframe: str = 'D',
                            archetype: str = 'standard',
                            instrument2: Optional[str] = None) -> Optional[str]:
    """
    Validate that a strategy generates enough signals on real data.
    Quick sanity check: try first param combo on recent data.
    Returns None if OK, error string if not.

    Minimum 5 signals: WF validation has 5 windows, even 1 signal/window
    is enough to compute meaningful returns. Validation gates (IS/WF/HO)
    will filter out bad strategies regardless of signal count.
    """
    import os
    from pathlib import Path
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        os.environ.setdefault('OANDA_ACCOUNT_ID', os.environ.get('OANDA_ACCOUNT_ID', ''))
        os.environ.setdefault('OANDA_API_TOKEN', os.environ.get('OANDA_API_TOKEN', ''))
        from data_fetcher import get_candles_date_range
    except Exception:
        return None  # Can't validate without data — skip

    ns = {}
    try:
        exec(code, ns)
    except Exception:
        return None  # let _validate_code catch this

    if 'generate_signals' not in ns:
        return None

    fn = ns['generate_signals']

    # Test on actual instrument/timeframe — use 6 months of 2019 data
    start, end = '2019-01-01', '2019-06-30'
    try:
        df = get_candles_date_range(instrument, start, end, granularity=timeframe)
    except Exception:
        return None  # data fetch issue — skip check

    if len(df) < 30:
        return None

    # Strip timezone from date column so LLM code can use df['date'].values safely
    if 'date' in df.columns and hasattr(df['date'].dtype, 'tz') and df['date'].dt.tz is not None:
        df = df.copy()
        df['date'] = df['date'].dt.tz_localize(None)

    # Inject archetype-specific columns (macro rates/yields, news, session, pair)
    # so a macro strategy referencing df['us10y'] etc. doesn't KeyError here.
    # The full validator injects these; the pre-check must match or every
    # non-standard-archetype strategy errors out at the signal sanity check.
    if archetype and archetype != 'standard':
        try:
            from supplementary_data import inject_supplementary_data
            df = inject_supplementary_data(df, archetype, instrument, instrument2,
                                           start, end, timeframe)
        except Exception:
            return None  # can't enrich — skip pre-check, full validator handles it

    # Try ALL param combos (up to 20) — accept if ANY combo fires enough signals.
    # This prevents false failures when the first combo is strict but a looser
    # combo (which the validator will naturally prefer) fires plenty of signals.
    from itertools import product as _product
    keys = list(param_grid.keys())
    values = [param_grid[k] if isinstance(param_grid[k], list) else [param_grid[k]] for k in keys]
    all_combos = [dict(zip(keys, combo)) for combo in _product(*values)]
    all_combos = all_combos[:30]  # cap at 30 to keep check fast

    best_count = 0
    last_error = None
    for params in all_combos:
        try:
            signals = fn(df, params)
            count = int((signals != 0).sum())
            if count > best_count:
                best_count = count
            if best_count >= min_signals:
                return None  # at least one combo passes — accept
        except Exception as e:
            last_error = f'runtime error: {type(e).__name__}: {e}'
            continue

    if best_count == 0 and last_error:
        return last_error
    return f'only {best_count} signals across all param combos (min {min_signals} needed)'


_VALID_FAMILIES = {
    'speed-based', 'cross-market', 'regime', 'flow-proxy',
    'event-driven', 'statistical', 'risk-factor',
}
# Map common LLM-generated family names to our canonical set
_FAMILY_ALIASES = {
    'breakout': 'regime',
    'trend': 'regime',
    'trend-following': 'regime',
    'momentum': 'regime',
    'mean-reversion': 'statistical',
    'mean_reversion': 'statistical',
    'reversion': 'statistical',
    'volatility': 'risk-factor',
    'volatility_breakout': 'regime',
    'volatility-breakout': 'regime',
    'calendar': 'statistical',
    'seasonal': 'statistical',
    'pattern': 'flow-proxy',
    'market-making': 'flow-proxy',
    'arbitrage': 'cross-market',
    'pairs': 'cross-market',
    'macro': 'risk-factor',
    'carry': 'risk-factor',
    'news': 'event-driven',
    'sentiment': 'event-driven',
    'microstructure': 'speed-based',
    'execution': 'speed-based',
}
_VALID_TIMEFRAMES = {'M30', 'H1', 'H4', 'D', 'W'}
# Timeframe keywords that suggest the model mixed timeframes in a single condition string
_TF_KEYWORDS = re.compile(
    r'\b(daily|weekly|hourly|H1|H4|D1|W1|4H|1H|1D|monthly)\b', re.IGNORECASE
)


# Distinctive proper-noun / ticker mentions in a rationale that PIN it to a
# specific instrument. v4-flash reaches for a canonical exemplar rationale
# (Bitcoin autocorrelation, WTI mean-reversion, gold safe-haven) and forgets to
# rewrite the narrative for the assigned instrument, producing "rationale about
# Bitcoin, trades USD/JPY" — ~18% of self-critique rejects, with the CORRECT
# instrument field (so the field-level guard can't see it). Each key here maps a
# keyword to the set of instruments it's legitimately ABOUT; a rationale
# containing the keyword while the slot is NOT in that set = cross-instrument
# bleed → drop pre-critique. Kept conservative (unambiguous names only) to avoid
# false positives on generic words.
_RATIONALE_INSTRUMENT_KEYWORDS = {
    'bitcoin':    {'BTC_USD'},
    'btc':        {'BTC_USD'},
    'ethereum':   {'ETH_USD'},
    'litecoin':   {'LTC_USD'},
    'wti':        {'WTICO_USD', 'BCO_USD'},
    'crude oil':  {'WTICO_USD', 'BCO_USD'},
    'crude':      {'WTICO_USD', 'BCO_USD'},
    'brent':      {'BCO_USD'},
    'natural gas':{'NATGAS_USD'},
    'natgas':     {'NATGAS_USD'},
    'ftse':       {'UK100_GBP'},
    'nasdaq':     {'NAS100_USD'},
    'nikkei':     {'JP225_USD'},
    'hang seng':  {'HK33_HKD'},
    'dax':        {'DE30_EUR'},
    'soybean':    {'SOYBN_USD'},
    'wheat':      {'WHEAT_USD'},
}


def _rationale_instrument_mismatch(rationale: str, instrument: str) -> Optional[str]:
    """Return the offending keyword if the rationale names a DIFFERENT specific
    instrument than the slot's, else None. Catches cross-instrument content bleed
    that carries the correct `instrument` field (invisible to the field guard)."""
    text = (rationale or '').lower()
    inst = (instrument or '').upper()
    for kw, owners in _RATIONALE_INSTRUMENT_KEYWORDS.items():
        if kw in text and inst not in owners:
            return kw
    return None


# Economically-linked default second leg, used when a pair thesis references
# close_leg2 but the model never named the second instrument (it treats
# close_leg2 as an abstract placeholder — 3 prompt fixes could not get v4-flash
# to emit the instrument2 field). Auto-assigning a sensible partner turns an
# otherwise-discarded pair into a validatable strategy. Prose extraction is
# tried first; this map is the fallback.
_PAIR_DEFAULT = {
    'XAU_USD': 'XAG_USD', 'XAG_USD': 'XAU_USD', 'XPT_USD': 'XAU_USD', 'XPD_USD': 'XPT_USD',
    'WTICO_USD': 'BCO_USD', 'BCO_USD': 'WTICO_USD', 'NATGAS_USD': 'WTICO_USD',
    'CORN_USD': 'WHEAT_USD', 'WHEAT_USD': 'CORN_USD', 'SOYBN_USD': 'CORN_USD',
    'BTC_USD': 'ETH_USD', 'ETH_USD': 'BTC_USD', 'LTC_USD': 'BTC_USD',
    'NAS100_USD': 'SPX500_USD', 'SPX500_USD': 'NAS100_USD', 'US30_USD': 'SPX500_USD',
    'DE30_EUR': 'SPX500_USD', 'AU200_AUD': 'SPX500_USD', 'JP225_USD': 'NAS100_USD',
    'UK100_GBP': 'DE30_EUR', 'HK33_HKD': 'CN50_USD', 'CN50_USD': 'HK33_HKD',
    'XCU_USD': 'AUD_USD', 'AUD_USD': 'NZD_USD', 'NZD_USD': 'AUD_USD',
    'EUR_USD': 'EUR_JPY', 'GBP_USD': 'EUR_USD', 'USD_CHF': 'EUR_USD', 'USD_JPY': 'EUR_JPY',
    'EUR_GBP': 'EUR_USD', 'EUR_JPY': 'USD_JPY', 'GBP_JPY': 'USD_JPY',
}
# Plain-name → OANDA symbol, for extracting an explicitly-named second leg from
# thesis prose ("gold/silver ratio" -> XAG_USD). DXY is deliberately absent — a
# DXY-second-leg thesis belongs to the macro archetype, not a pair.
_NAME_TO_INSTRUMENT = {
    'silver': 'XAG_USD', 'gold': 'XAU_USD', 'copper': 'XCU_USD', 'platinum': 'XPT_USD',
    'palladium': 'XPD_USD', 'brent': 'BCO_USD', 'wti': 'WTICO_USD', 'crude': 'WTICO_USD',
    'natural gas': 'NATGAS_USD', 'natgas': 'NATGAS_USD', 'corn': 'CORN_USD',
    'wheat': 'WHEAT_USD', 'soybean': 'SOYBN_USD', 'bitcoin': 'BTC_USD',
    'ethereum': 'ETH_USD', 'litecoin': 'LTC_USD', 'nasdaq': 'NAS100_USD',
    's&p': 'SPX500_USD', 'nikkei': 'JP225_USD', 'dax': 'DE30_EUR', 'ftse': 'UK100_GBP',
    'hang seng': 'HK33_HKD', 'a50': 'CN50_USD',
}


def _infer_instrument2(text: str, primary: str) -> Optional[str]:
    """Best-guess second leg for a pair thesis whose instrument2 field is empty.
    (1) an explicit OANDA symbol in the text, (2) a named instrument ('silver'),
    (3) the curated economically-linked default. Returns None if none applies
    (e.g. the primary has no natural partner)."""
    primary = (primary or '').upper()
    raw = text or ''
    for m in re.findall(r'\b([A-Z]{2,6}_[A-Z]{3})\b', raw):     # explicit symbol
        if m != primary and (m in _PAIR_DEFAULT or m in _PAIR_DEFAULT.values()):
            return m
    low = raw.lower()
    for kw, inst in _NAME_TO_INSTRUMENT.items():                # named instrument
        if kw in low and inst != primary:
            return inst
    return _PAIR_DEFAULT.get(primary)                           # curated fallback


def _validate_thesis(thesis: dict) -> Optional[str]:
    """
    Validate a single thesis dict returned by the LLM.

    Returns an error string describing the first problem found,
    or None if the thesis is usable.
    """
    if not isinstance(thesis, dict):
        return 'thesis is not a dict'

    # 1. Required string fields — must exist and be non-empty
    required_str = [
        'strategy_family', 'timeframe',
        'rationale', 'entry_condition', 'filter_condition', 'exit_condition',
    ]
    for key in required_str:
        val = thesis.get(key, '')
        if not isinstance(val, str) or not val.strip():
            return f'missing or empty field: {key!r}'

    # 1b. Cross-market/pair thesis must declare the instrument2 FIELD — a strategy
    # that trades a second leg needs it to load, or the whole thing aborts at
    # 'No valid data' (this was 255/259 cross-market failures). Catch BOTH the
    # literal close_leg2 form AND the prose form (family=cross-market, or the
    # conditions mention spread/ratio/divergence/leg2) — the model often names
    # the second market only in entry text and forgets the structured key.
    # Triggers are DEFINITIVE only — the cross-market/pair FAMILY, or the leg2
    # columns (which exist ONLY for pairs). Deliberately NOT 'spread' (bid-ask
    # microstructure), 'ratio' (efficiency ratio is a common single-instrument
    # detector), or 'divergence' (MACD/RSI divergence) — those false-positive on
    # single-instrument theses.
    _fam = str(thesis.get('strategy_family', '')).lower()
    _conds = ' '.join(thesis.get(k, '') for k in
                      ('entry_condition', 'filter_condition', 'exit_condition')).lower()
    _is_pair = ('cross-market' in _fam or _fam == 'pair'
                or 'close_leg2' in _conds or 'close_leg1' in _conds or 'leg2' in _conds)
    if _is_pair and not str(thesis.get('instrument2', '')).strip():
        # The model routinely omits instrument2 — auto-assign a sensible second
        # leg (prose extraction, else the economically-linked default) so the
        # pair becomes validatable instead of discarded. Only reject if the
        # primary has no natural partner.
        _i2 = _infer_instrument2(_conds + ' ' + thesis.get('rationale', ''),
                                 thesis.get('instrument', ''))
        if _i2:
            thesis['instrument2'] = _i2
        else:
            return 'cross-market/pair thesis missing the instrument2 field'

    # 2. strategy_family must be from the allowed set (normalize aliases first)
    family = thesis['strategy_family'].strip().lower().replace(' ', '-')
    family = _FAMILY_ALIASES.get(family, family)
    if family not in _VALID_FAMILIES:
        return f'unknown strategy_family {thesis["strategy_family"]!r} (must be one of {sorted(_VALID_FAMILIES)})'
    thesis['strategy_family'] = family  # normalize in-place

    # 3. timeframe must be valid
    tf = thesis['timeframe'].strip().upper()
    if tf not in _VALID_TIMEFRAMES:
        return f'invalid timeframe {thesis["timeframe"]!r} (must be M30/H1/H4/D/W)'

    # 4. Conditions must be specific enough (reject blank / trivially short strings)
    # 10-char minimum: catches empty/null conditions while allowing precise short ones
    # like "ADX(14) > 20" (13 chars) or "exit when z-score crosses 0" (27 chars)
    for key in ('entry_condition', 'filter_condition', 'exit_condition'):
        val = thesis[key].strip()
        if len(val) < 10:
            return f'{key!r} is too short/vague (< 10 chars): {val!r}'

    # 4b. The exit must express the thesis ENDING, not merely elapsing (2026-08-03).
    # Measured on the 2026-08-03 batch: 9 of 14 theses exited on a bare bar count
    # unrelated to their own entry mechanism, and those scored WF 0.00-0.09 against
    # a 0.5 floor. A fixed horizon cannot express why an edge stops, so it is not a
    # thesis. The prompt now forbids it; this makes the rule binding rather than
    # advisory. DELIBERATELY CONSERVATIVE: a bar count is rejected ONLY when it is
    # the SOLE exit — a compound exit ("after 3 bars OR when the gap is 50% filled")
    # carries a mechanism and passes, as does any ATR/indicator/level exit. Over-
    # rejecting here is expensive (a discarded thesis costs a whole iteration), so
    # the mechanism vocabulary is broad and the bar-count pattern is narrow.
    _exit = thesis['exit_condition'].strip().lower()
    # Qualifier words may appear on EITHER side of the count: "after 6 H4 bars"
    # slipped through on 2026-08-03 because the original pattern allowed a word
    # only BEFORE the number. Both sides are now repeatable (`*`), which also
    # covers "after 3 full trading days" and "within the next 5 daily bars".
    _bar_count = re.search(r'\b(?:after|within|for)\s+'
                           r'(?:\w+\s+)*(?:\d+|n|exit_bars|hold\w*|\w*_bars)\s+'
                           r'(?:\w+\s+)*(?:bar|day|session|period|candle)s?\b', _exit)
    # Price-STRUCTURE words (high/low/level/breakout/quantile/range/pivot) are in the
    # vocabulary too: constraint [5] of the standard pool asks for a quantile-breakout
    # entry, whose natural exit is "closes back below the 20-bar high" — a genuine
    # mechanism that the indicator-only vocabulary rejected (found 2026-08-04). Left
    # OUT deliberately: bare "close"/"price", which nearly every exit string contains
    # and which would make the bar-count branch unreachable.
    _mechanism = re.search(
        r'\b(atr|stop|trail|cross(?:es|ing)?|revert|reclaim|z-?score|rsi|adx|sma|ema|'
        r'donchian|channel|band|midpoint|slope|spread|ratio|percentile|median|mean|'
        r'fill(?:ed)?|target|opposite|signal\s+flip|flips?|'
        r'high|low|level|break(?:s|out|down|ing)?|quantile|range|pivot|extreme|'
        r'breakeven|entry\s+price)\b', _exit)
    if _bar_count and not _mechanism:
        return (f'exit_condition is a bare bar count with no mechanism — it must read the '
                f'quantity the entry is built on (indicator cross, level reclaimed, spread '
                f'reverted), optionally WITH a timeout: {thesis["exit_condition"]!r}')

    # 5. param_hints must be a dict with at least one list of values
    hints = thesis.get('param_hints', {})
    if not isinstance(hints, dict) or not hints:
        return 'param_hints is missing or empty'
    has_list = any(isinstance(v, list) and len(v) > 0 for v in hints.values())
    if not has_list:
        return 'param_hints has no list values — model must provide sweep ranges'

    # 6. Detect mixed-timeframe references inside a single strategy
    #    (e.g. entry says "daily" but timeframe is H1 — would cause lookback confusion)
    all_conditions = ' '.join([
        thesis.get('entry_condition', ''),
        thesis.get('filter_condition', ''),
        thesis.get('exit_condition', ''),
    ])
    tf_hits = _TF_KEYWORDS.findall(all_conditions)
    if len(set(t.upper() for t in tf_hits)) > 1:
        return (f'conditions reference multiple timeframe keywords {tf_hits} — '
                f'pick ONE timeframe and express higher-TF context as longer windows')

    return None  # thesis is valid


_SELF_CRITIQUE_SYSTEM = (
    "You are a skeptical senior quant reviewing a junior researcher's strategy "
    "thesis BEFORE any code is written or backtested. Your ONLY job is to catch "
    "fatal DESIGN flaws — NOT to predict whether it will be profitable.\n\n"
    "Reject ONLY if the thesis has a clear, specific, fatal flaw in one of:\n"
    "1. MECHANISM: the economic rationale is a post-hoc label with no real driver "
    "(an arbitrary indicator dressed up with a 'because traders...' story). A "
    "vague-but-plausible economic story is FINE — pass it.\n"
    "2. FIDELITY: the entry/exit logic contradicts the stated mechanism — e.g. "
    "rationale says mean-REVERSION but the entry buys breakouts (continuation), "
    "or claims a reversal yet rides the move.\n"
    "3. REGIME INDEPENDENCE: reject ONLY if the filter restates the SAME "
    "CONDITION as the entry (e.g. entry 'ADX>25' AND filter 'ADX>25'). Computing "
    "the gate from the same price series is NOT circular by itself — a gate that "
    "measures a DIFFERENT property is independent and VALID. The standard regime "
    "detectors this system REQUIRES are all valid even though derived from price: "
    "volatility/ATR regime, return autocorrelation (ranging vs trending), "
    "efficiency ratio, Hurst, MA-slope or MA-separation, distance-from-mean, plus "
    "calendar/session and spread/liquidity. Do NOT reject a regime gate merely "
    "for sharing the price series with the entry — when unsure, PASS. A gate is "
    "'redundant' ONLY when it recomputes the entry's LITERAL condition — NOT when "
    "it confirms the market state the entry's RATIONALE merely assumes: e.g. an "
    "autocorrelation or efficiency-ratio gate confirming a trending regime for a "
    "trend-following entry is a VALID independent filter, not a circular restatement.\n"
    "4. LOOK-AHEAD: reject only if the logic needs information unavailable at "
    "decision time — future bars, not-yet-published data (an economic figure used "
    "before its release), or acting DURING the bar it is still measuring. A signal "
    "computed from a COMPLETED bar's own OHLC (close-vs-open range, close vs its "
    "SMA, etc.) and acted on the NEXT bar is standard and NOT look-ahead — do NOT "
    "reject for that. A POSITIVE pandas shift such as `x.shift(10)` references a "
    "PAST value (10 bars ago) and is NEVER look-ahead; only a NEGATIVE shift "
    "(`x.shift(-k)`), an explicit future index, or using the still-forming bar "
    "qualifies — do NOT call a positive .shift() look-ahead.\n\n"
    "Default to PASS. Do NOT reject for being simple, common, low-edge, or "
    "'might not work' — the backtest validator judges performance independently. "
    "Reject only on a structural design defect you can name in ONE specific "
    "sentence.\n\n"
    'Reply with ONLY this JSON: {"verdict":"pass"|"reject","reason":"one specific sentence"}'
)


def _repair_thesis_field(thesis: dict, err: str, instrument: str,
                         api_key: str = None, _call=None) -> Optional[dict]:
    """Ask the model to fix ONLY the field _validate_thesis rejected.

    Added 2026-08-03. A structurally-invalid thesis used to end its iteration
    outright, which got materially worse once the exit-mechanism rule landed —
    the thesis is normally sound apart from the one bad field, so re-rolling the
    whole idea wastes the good part along with the bad.

    TARGETED ON PURPOSE: the prompt carries the exact violation and forbids
    touching anything else, so a rejected EXIT is repaired without the model
    quietly reworking the entry to something easier. Returns None on any failure
    (no key, API error, unparseable, or a reply that drops fields) and the caller
    keeps the original — so this can only add a chance to recover, never corrupt
    a thesis that was merely mis-shaped.
    """
    key = api_key or OPENROUTER_API_KEY
    if not key and _call is None:
        return None
    prompt = (
        f"This trading-strategy thesis for {instrument} was REJECTED by a validator.\n\n"
        f"REJECTION: {err}\n\n"
        f"Current thesis:\n{json.dumps(thesis, indent=2)}\n\n"
        "Fix ONLY what the rejection names. Do NOT change the mechanism, the "
        "instrument, the family, the timeframe, or any field the rejection does "
        "not mention — the idea is approved, only its shape is wrong.\n"
        "Reply with ONLY the corrected JSON object, same keys as above."
    )
    try:
        call = _call or (lambda p: call_openrouter(
            system_prompt='You repair malformed strategy-thesis JSON. Output ONLY JSON.',
            user_prompt=p, model=_chain_order(THESIS_MODELS)[0], api_key=key,
            temperature=0.2, max_tokens=THESIS_SINGLE_MAX_TOKENS))
        res = call(prompt)
        if not res or not res.get('success'):
            return None
        fixed = res.get('candidate')
        if not isinstance(fixed, dict):
            return None
        # Never let a repair DROP fields — merge over the original.
        merged = {**thesis, **fixed}
        # The repair may not change identity; those are ours, not the model's.
        for k in ('instrument', 'instrument2', 'timeframe'):
            if k in thesis:
                merged[k] = thesis[k]
        return merged
    except Exception:
        return None


_CRITIQUE_LOG = os.getenv(
    'CRITIQUE_LOG', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '.auto-research-logs', 'critique_theses.jsonl'))


def _record_critique(thesis: dict, instrument: str, crit: dict) -> None:
    """Append the FULL thesis and its critique verdict to a JSONL corpus.

    stdout truncates every field to 80 chars and a rejected thesis never becomes
    a `strategies` row, so a rejected design was previously unrecoverable — which
    made it impossible to tell a stricter critic apart from worse theses when the
    reject rate moved. Passes are recorded too: without them there is no control
    arm. Best-effort; a write failure must never break a batch.
    """
    try:
        os.makedirs(os.path.dirname(_CRITIQUE_LOG), exist_ok=True)
        with open(_CRITIQUE_LOG, 'a') as fh:
            fh.write(json.dumps({
                'at': datetime.now(timezone.utc).isoformat(),
                'instrument': instrument,
                # the configured head, not necessarily the one that served it —
                # the chain falls through on error and does not report which won
                'critique_head': (SELF_CRITIQUE_MODELS or [None])[0],
                'thesis_head': (THESIS_MODELS or [None])[0],
                'verdict': crit.get('verdict'),
                'reason': crit.get('reason'),
                'thesis': thesis,
            }) + '\n')
    except Exception as e:
        print(f"  (critique corpus write skipped: {e})", flush=True)


def self_critique_thesis(thesis: dict, instrument: str, api_key: str = None, _call=None) -> dict:
    """Design-quality reflection gate (see SELF_CRITIQUE_ENABLED comment).

    Returns {'verdict': 'pass'|'reject', 'reason': str}. ALWAYS fails open:
    any LLM error, unparseable output, or exception yields 'pass' so a flaky
    API can never starve the batch. `_call` is injectable for testing.
    """
    _call = _call or call_openrouter
    user = (
        f"Instrument: {instrument}\n"
        f"Strategy family: {thesis.get('strategy_family', '')}\n"
        f"Timeframe: {thesis.get('timeframe', '')}\n"
        f"Rationale (claimed mechanism): {thesis.get('rationale', '')}\n"
        f"Entry: {thesis.get('entry_condition', '')}\n"
        f"Filter / regime gate: {thesis.get('filter_condition', '')}\n"
        f"Exit: {thesis.get('exit_condition', '')}\n"
    )
    try:
        res = {'success': False, 'error': 'no critique model configured'}
        for model in _chain_order(SELF_CRITIQUE_MODELS):
            res = _call(system_prompt=_SELF_CRITIQUE_SYSTEM, user_prompt=user,
                        model=model, api_key=api_key,
                        temperature=SELF_CRITIQUE_TEMPERATURE, max_tokens=SELF_CRITIQUE_MAX_TOKENS)
            if res.get('success'):
                break
        if not res.get('success'):
            return {'verdict': 'pass', 'reason': f"critique unavailable, fail-open: {str(res.get('error'))[:80]}"}
        cand = res.get('candidate')
        if not isinstance(cand, dict):
            return {'verdict': 'pass', 'reason': 'critique returned non-dict, fail-open'}
        verdict = str(cand.get('verdict', '')).strip().lower()
        reason = str(cand.get('reason', ''))[:200]
        if verdict == 'reject':
            return {'verdict': 'reject', 'reason': reason or 'design flaw (no reason given)'}
        return {'verdict': 'pass', 'reason': reason}
    except Exception as e:
        return {'verdict': 'pass', 'reason': f'critique exception, fail-open: {str(e)[:80]}'}


def post_codegen_fidelity_critique(thesis: dict, candidate: dict, instrument: str, api_key: str = None, _call=None) -> dict:
    _call = _call or call_openrouter
    user = (
        f"Instrument: {instrument}\n"
        f"Approved strategy family: {thesis.get('strategy_family', '')}\n"
        f"Approved timeframe: {thesis.get('timeframe', '')}\n"
        f"Approved rationale: {thesis.get('rationale', '')}\n"
        f"Approved entry: {thesis.get('entry_condition', '')}\n"
        f"Approved filter / regime gate: {thesis.get('filter_condition', '')}\n"
        f"Approved exit: {thesis.get('exit_condition', '')}\n\n"
        f"Generated archetype: {candidate.get('archetype', '')}\n"
        f"Generated param_grid: {json.dumps(candidate.get('param_grid', {}), ensure_ascii=False)}\n"
        f"Generated code:\n```python\n{candidate.get('code', '')}\n```\n"
    )
    system = (
        "You are checking whether generated trading-strategy code faithfully implements an approved thesis. "
        "Reject only for a CLEAR mismatch: invented entry logic, invented regime gate, exit contradicts the thesis, "
        "or code ignores the thesis mechanism. Do NOT reject for implementation detail, indicator choice synonyms, "
        "or harmless threshold/grid differences. Default to PASS when unsure. "
        'Reply with ONLY this JSON: {"verdict":"pass"|"reject","reason":"one specific sentence"}'
    )
    try:
        res = {'success': False, 'error': 'no critique model configured'}
        for model in _chain_order(SELF_CRITIQUE_MODELS):
            res = _call(system_prompt=system, user_prompt=user,
                        model=model, api_key=api_key,
                        temperature=SELF_CRITIQUE_TEMPERATURE, max_tokens=SELF_CRITIQUE_MAX_TOKENS)
            if res.get('success'):
                break
        if not res.get('success'):
            return {'verdict': 'pass', 'reason': f"critique unavailable, fail-open: {str(res.get('error'))[:80]}"}
        cand = res.get('candidate')
        if not isinstance(cand, dict):
            return {'verdict': 'pass', 'reason': 'critique returned non-dict, fail-open'}
        verdict = str(cand.get('verdict', '')).strip().lower()
        reason = str(cand.get('reason', ''))[:200]
        if verdict == 'reject':
            return {'verdict': 'reject', 'reason': reason or 'code/thesis mismatch'}
        return {'verdict': 'pass', 'reason': reason}
    except Exception as e:
        return {'verdict': 'pass', 'reason': f'critique exception, fail-open: {str(e)[:80]}'}


def _extract_json(text: str):
    """Try to extract JSON from LLM output (supports fenced markdown, arrays, and objects)."""
    text = text.strip()

    # Handle fenced blocks like ```json ... ``` and ``` ... ```
    if text.startswith('```'):
        lines = text.splitlines()
        if lines:
            first = lines[0].strip().lower()
            if first in ('```json', '```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            text = '\n'.join(lines).strip()

    # If model returned multiple fenced blocks, grab first json-looking block
    if '```' in text:
        for block in text.split('```'):
            candidate = block.strip()
            if not candidate:
                continue
            if candidate.lower().startswith('json'):
                candidate = candidate[4:].strip()
            if (candidate.startswith('{') and candidate.endswith('}')) or \
               (candidate.startswith('[') and candidate.endswith(']')):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON array first (for batch responses), then object
    arr_start = text.find('[')
    arr_end = text.rfind(']') + 1
    obj_start = text.find('{')
    obj_end = text.rfind('}') + 1

    # Prefer whichever comes first in the text
    if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
        if arr_end > arr_start:
            try:
                return json.loads(text[arr_start:arr_end])
            except json.JSONDecodeError:
                pass

    if obj_start >= 0 and obj_end > obj_start:
        try:
            return json.loads(text[obj_start:obj_end])
        except json.JSONDecodeError:
            pass

    return None


# ============================================================================
# AUTO RESEARCH LOOP
# ============================================================================

class AutoResearcher:
    """Automated research loop: generate → validate → record → repeat."""

    # Multi-instrument pool for diversity (FX majors, crosses, commodities)
    DEFAULT_INSTRUMENT_POOL = [
        'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF',
        'AUD_USD', 'NZD_USD', 'EUR_GBP', 'EUR_JPY', 'GBP_JPY',
        'XAU_USD', 'XAG_USD', 'BCO_USD', 'WTICO_USD',
        'NATGAS_USD', 'CORN_USD', 'SOYBN_USD', 'WHEAT_USD',
        'BTC_USD', 'ETH_USD', 'LTC_USD',
        # Equity indices (core)
        'SPX500_USD', 'NAS100_USD', 'DE30_EUR', 'UK100_GBP',
        'JP225_USD', 'AU200_AUD',
        # Other metals
        'XCU_USD', 'XPT_USD', 'XPD_USD',
        # Asian indices
        'HK33_HKD', 'CN50_USD',
    ]

    def __init__(
        self,
        instruments: List[str] = None,
        model: str = DEFAULT_MODEL,
        api_key: str = None,
        temperature: float = 0.7,
        min_delay_seconds: float = 2.0
    ):
        self.instruments = instruments or self.DEFAULT_INSTRUMENT_POOL
        self.model = model
        self.api_key = api_key or OPENROUTER_API_KEY
        self.temperature = temperature
        self.min_delay = min_delay_seconds
        # Per-batch random start offset into the instrument pool. Set fresh each
        # run() so the target-reached early-stop doesn't always sample the front
        # of the pool (which over-sampled NZD at position 6 and starved the
        # later indices/metals). 0 until run() randomises it.
        self._pool_offset = 0

        # Ensure DB and candidate dir exist
        pu.init_db()
        CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)

    def _rotate_instrument(self, iteration: int) -> str:
        # Must match the batch schedule's indexing in _generate_thesis_batch
        # (instruments[(i-1+pool_offset) % len]). Iterations are 1-based, so
        # iteration 1 maps to instruments[pool_offset]. The per-batch random
        # offset spreads coverage across the whole pool despite the early-stop.
        offset = getattr(self, '_pool_offset', 0)
        return self.instruments[(iteration - 1 + offset) % len(self.instruments)]

    def _generate_strategy_id(self, prefix: str, iteration: int) -> str:
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        return f'{prefix}_auto_{ts}_i{iteration}'

    def _check_duplicate(self, candidate: Dict) -> Optional[str]:
        """Return existing status if fingerprint exists, else None."""
        code = candidate.get('code', '')
        param_grid = candidate.get('param_grid', {})
        fp = pu.compute_strategy_fingerprint(
            code,
            param_grid,
            candidate.get('timeframe', 'D'),
            candidate.get('instrument', ''),
            candidate.get('archetype', 'standard'),
        )
        existing = pu.check_idea_is_new(fp)
        if not existing['new']:
            return existing.get('status', 'unknown')
        return None

    def _save_candidate(self, candidate: Dict, iteration: int) -> Path:
        """Save candidate JSON to disk."""
        fp = CANDIDATE_DIR / f'candidate_{iteration:03d}.json'
        with open(fp, 'w') as f:
            json.dump(candidate, f, indent=2)
        return fp

    def _validate_candidate(self, candidate: Dict) -> tuple:
        """Run validator on candidate. Returns (passed: bool, message: str)."""
        try:
            # Ensure instrument is set
            if 'instrument' not in candidate:
                candidate['instrument'] = self.instruments[0]
            return validate_strategy(candidate)
        except Exception as e:
            return False, f'Validator exception: {e}'

    def _get_scores(self, strategy_id: str) -> Dict[str, float]:
        """Get validation scores for a strategy from DB."""
        with pu.get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                'SELECT is_gt_score, walk_forward_gt_score, holdout_gt_score, best_params FROM validation_results WHERE strategy_id = ?',
                (strategy_id,)
            )
            row = c.fetchone()
            if row:
                import json as _json
                bp = {}
                try:
                    bp = _json.loads(row['best_params']) if row['best_params'] else {}
                except Exception:
                    pass
                return {
                    'is_score': row['is_gt_score'] or 0.0,
                    'wf_score': row['walk_forward_gt_score'] or 0.0,
                    'ho_score': row['holdout_gt_score'] or 0.0,
                    'best_params': bp,
                }
            return {'is_score': 0.0, 'wf_score': 0.0, 'ho_score': 0.0, 'best_params': {}}

    def run(
        self,
        target_passed: int = 3,
        max_iterations: int = 30,
        instruments: List[str] = None
    ) -> Dict[str, Any]:
        """
        Run the auto-research loop.

        Args:
            target_passed: Stop after this many strategies pass validation
            max_iterations: Maximum LLM calls before giving up
            instruments: Override instruments list

        Returns:
            Summary dict: {
                'iterations': int,
                'passed': [id, ...],
                'failed': [id, ...],
                'errors': int,
                'duration_seconds': float
            }
        """
        if instruments:
            self.instruments = instruments

        # Randomise the pool start position for THIS batch so the target-reached
        # early-stop doesn't keep sampling the front of the pool (which flooded
        # the book with NZD at position 6 and starved indices/metals at 21-31).
        self._pool_offset = random.randint(0, len(self.instruments) - 1)

        results = {
            'iterations': 0,
            'passed': [],
            'failed': [],
            'errors': 0,
            'guarded': 0,         # theses a deterministic guard skipped pre-validation
                                  # (rationale bleed, pair-without-instrument2) — NOT real
                                  # errors; broken out so 'errors' stays meaningful
            'critiqued_out': 0,   # theses the self-critique gate rejected pre-codegen
            'fingerprint_rejected': 0,  # theses that contradicted the instrument's measured structure
            'start_time': datetime.utcnow().isoformat(),
        }
        start = time.time()

        print(f"\n{'='*70}")
        print(f"Auto Research Loop")
        print(f"Target: {target_passed} passed | Max iterations: {max_iterations}")
        print(f"Instruments: {self.instruments} | Model: {self.model}")
        print(f"{'='*70}\n")

        # ── Pre-generate all theses in one batch CLI call ─────────────────────
        # Load shared context once (program.md phase, failed strategies)
        _failed_for_batch = pu.get_failed_strategies()
        _failed_ctx_batch = ""
        if _failed_for_batch:
            lines = ["Previously failed strategies (do not repeat):"]
            for fs in _failed_for_batch[:5]:
                lines.append(f"- {fs.get('rationale', '')[:120]}")
            _failed_ctx_batch = "\n".join(lines) + "\n\n"
        _phase_batch = ""
        _rp = _get_research_phase()
        if _rp:
            _phase_batch = f"\nCURRENT RESEARCH DIRECTIVES (follow these):\n{_rp}\n"

        thesis_batch = _generate_thesis_batch(
            instruments=self.instruments,
            max_iterations=max_iterations,
            failed_ctx=_failed_ctx_batch,
            phase_block=_phase_batch,
            pool_offset=self._pool_offset,
        )
        # ──────────────────────────────────────────────────────────────────────

        for iteration in range(1, max_iterations + 1):
            results['iterations'] = iteration

            if len(results['passed']) >= target_passed:
                print(f"\n✓ Target reached: {len(results['passed'])} strategies passed")
                break

            instrument = self._rotate_instrument(iteration)

            try:
                # Step 1: Query DB for failures
                failed = pu.get_failed_strategies()

                # Step 2: Build prompts (old single-step flow — kept for reference but not used)
                # system_prompt = _build_system_prompt()
                # user_prompt = _build_user_prompt(instrument, failed, iteration)

                # Step 3: Call LLM - Two-step generation
                # Step A: Generate thesis via free OpenRouter model
                # Step B: Generate code via OpenRouter
                # ── Creative constraint label (for logging) ────────────────────
                wild = (iteration % 8 == 0)
                macro = (iteration % 3 == 0) and not wild
                asset_constraint = None
                # asset/calendar forcing dialed back to match the batch schedule
                # (i%5 -> i%9) — calendar seasonals yield ~0 durable passes.
                nnfx = (not wild) and (not macro) and (iteration % 12 == 7)
                if not wild and not macro and not nnfx and (iteration % 9 == 0):
                    asset_constraint = _asset_mode_for(instrument)
                asset = asset_constraint is not None
                constraint = _CREATIVE_CONSTRAINTS[iteration % len(_CREATIVE_CONSTRAINTS)]
                detector = None if wild else _REGIME_DETECTORS[iteration % len(_REGIME_DETECTORS)]
                # Asset/event slots pinned to D (see _build_batch_schedule):
                # event-timing columns are day-resolution, broken on weekly.
                _is_event = 'days_to_event' in constraint or 'event_window' in constraint
                if wild:
                    tf_forced = None
                elif asset or _is_event:
                    tf_forced = 'D'
                else:
                    # Same rotation the batch path uses, so a steering.md edit
                    # governs BOTH paths (this is the per-iteration fallback
                    # taken when the batch cascade could not supply a thesis).
                    import steering as _st
                    _tfr = _st.load().timeframe_rotation or _TIMEFRAME_ROTATION
                    tf_forced = _tfr[(iteration - 1) % len(_tfr)]
                if wild:
                    constraint = (
                        "WILD MODE: Ignore conventional strategy families. "
                        "Propose something structurally different from anything tried before — "
                        "unusual timeframe, non-standard entry logic, exotic exit rule."
                    )
                elif macro:
                    constraint = _macro_constraint_for(instrument)
                elif nnfx:
                    constraint = _NNFX_CONSTRAINT
                    detector = None  # the multi-layer filter IS the regime gate
                elif asset:
                    constraint = asset_constraint
                mode_label = ("WILD" if wild else "MACRO" if macro
                              else "NNFX" if nnfx
                              else "ASSET" if asset
                              else f"constraint[{iteration % len(_CREATIVE_CONSTRAINTS)}]")

                print(f"\n[Iteration {iteration}/{max_iterations}] {instrument}", flush=True)
                print(f"  Step A: Generating thesis...", flush=True)

                # ── Try pre-generated batch thesis first ───────────────────────
                thesis_result = None
                _batch_item = thesis_batch[iteration - 1] if thesis_batch and (iteration - 1) < len(thesis_batch) else None
                if _batch_item is not None:
                    thesis_result = {'success': True, 'candidate': _batch_item, 'error': None}
                    # The batch stamped the slot that really ran. mode_label above
                    # is recomputed from the iteration and is only correct for the
                    # per-iteration fallback below, so prefer the stamp.
                    print(f"  [{_batch_item.get('_slot_label', mode_label)}] "
                          f"thesis from batch ✓", flush=True)
                else:
                    print(f"  [{mode_label}] {constraint[:80]}...", flush=True)

                # ── Fall back to single-iteration OpenRouter generation ─────────
                # ── Fall back to single-iteration OpenRouter thesis generation ──
                if thesis_result is None:
                    failed_ctx = ""
                    if failed:
                        lines = ["Previously failed strategies (do not repeat):"]
                        for fs in failed[:5]:
                            lines.append(f"- {fs.get('rationale', '')[:120]}")
                        failed_ctx = "\n".join(lines) + "\n\n"
                    research_phase = _get_research_phase()
                    phase_block = f"\nCURRENT RESEARCH DIRECTIVES (follow these):\n{research_phase}\n" if research_phase else ""

                    _detector_line = (
                        f"\n\nREGIME DETECTOR FOR THIS ITERATION: {detector}\n"
                        "The filter_condition MUST be built from this exact detector — "
                        "do NOT substitute ADX or another detector. It is an INDEPENDENT "
                        f"regime gate: the entry_condition must fire on a DIFFERENT signal, "
                        f"NOT restate {detector}. A filter that repeats the entry is REJECTED."
                    ) if detector else ""
                    _tf_line = (
                        f"\n\nTIMEFRAME FOR THIS ITERATION: {tf_forced}\n"
                        f"Set 'timeframe' to {tf_forced} and design every lookback/window "
                        f"for {tf_forced} bars — do NOT default to daily."
                    ) if tf_forced else ""
                    thesis_system = (
                        "You are a quantitative trading researcher. "
                        "Output ONLY valid JSON. No explanation, no preamble, no markdown.\n\n"
                        + _get_thesis_rules()
                        + "\n\nCONSTRAINT FOR THIS ITERATION: " + constraint
                        + _detector_line
                        + _tf_line
                    )
                    thesis_prompt = (
                        f"Instrument: {instrument}\n"
                        f"{phase_block}"
                        f"{failed_ctx}"
                        "Pick a STRATEGY FAMILY and design a precise trading strategy spec. The family "
                        "must name the mechanism the ENTRY uses, not the filter bolted on to it —\n"
                        "  speed-based: rate of change, time-of-day/session microstructure\n"
                        "  cross-market: a SECOND instrument (spread, ratio, lead-lag); needs instrument2\n"
                        "  regime:       trend/range state — breakouts, channels, MA structure\n"
                        "  flow-proxy:   a flow footprint — gaps, range extension, volume\n"
                        "  event-driven: a scheduled EVENT or calendar date\n"
                        "  statistical:  a distributional property — z-score, RSI extreme, dispersion\n"
                        "  risk-factor:  a MACRO driver — rates, real yields, the dollar, carry\n\n"
                        "CRITICAL: ALL conditions must use the SAME single timeframe. "
                        "Do NOT mix D/H4/W/H1 — pick one timeframe and use it for everything.\n\n"
                        "CRITICAL: the exit_condition MUST reference the SAME quantity the entry is "
                        "built on, so the exit expresses the thesis ENDING rather than merely elapsing. "
                        "A bare bar count ('exit after N bars') as the ONLY exit is REJECTED; it is "
                        "allowed only as a secondary timeout beside a mechanism exit. An ATR or "
                        "volatility stop DOES satisfy this — only a BARE HORIZON fails.\n\n"
                        "Reply with ONLY this JSON and nothing else:\n"
                        "{\n"
                        '  "strategy_family": "regime",\n'
                        '  "timeframe": "D",\n'
                        '  "rationale": "One sentence — WHY this edge exists economically.",\n'
                        '  "entry_condition": "Exact measurable entry: indicator, threshold, lookback.",\n'
                        '  "filter_condition": "Regime or vol filter with exact thresholds.",\n'
                        '  "exit_condition": "Exit that READS THE ENTRY\'S OWN quantity — indicator '
                        'cross, level reclaimed, spread reverted. A bare bar count alone is REJECTED.",\n'
                        '  "param_hints": {"lookback": [10, 20, 30], "threshold": [0.5, 1.0, 1.5]}\n'
                        "}"
                    )
                    # Iterate the env-configured THESIS_MODELS chain (the same
                    # list the batch cascade iterates). On a rate-limit, sleep +
                    # retry the SAME model once before advancing, so a transient
                    # 429 on the primary doesn't immediately burn a paid slot.
                    thesis_result = {'success': False, 'error': 'no thesis model attempted'}
                    for _mdl in _chain_order(THESIS_MODELS):
                        thesis_result = call_openrouter(
                            system_prompt=thesis_system,
                            user_prompt=thesis_prompt,
                            model=_mdl,
                            api_key=self.api_key,
                            temperature=0.7,
                            # Reasoning headroom — every model in the chain
                            # returned empty content at 600 (measured
                            # 2026-07-23), which made this regeneration path a
                            # guaranteed no-op. All three pass at 2500.
                            max_tokens=THESIS_SINGLE_MAX_TOKENS,
                        )
                        if thesis_result['success']:
                            break
                        err = thesis_result['error']
                        if '429' in err or 'rate' in err.lower():
                            wait = 30
                            m = re.search(r'retry_after_seconds["\s:]+(\d+)', err)
                            if m:
                                wait = int(m.group(1)) + 2
                            print(f"  ! Rate limited on {_mdl}, waiting {wait}s and retrying...")
                            time.sleep(wait)
                            thesis_result = call_openrouter(
                                system_prompt=thesis_system,
                                user_prompt=thesis_prompt,
                                model=_mdl,
                                api_key=self.api_key,
                                temperature=0.7,
                                max_tokens=THESIS_SINGLE_MAX_TOKENS,
                            )
                            if thesis_result['success']:
                                break
                        print(f"  ! Thesis model {_mdl} failed — trying next in chain...", flush=True)

                # A batch thesis succeeded above; only a failed single-iteration
                # thesis reaches here (rate-limit retry + fallback are handled
                # inside the THESIS_MODELS chain loop above).
                if not thesis_result['success']:
                    print(f"  ✗ Thesis error: {thesis_result['error']}")
                    results['errors'] += 1
                    time.sleep(self.min_delay)
                    continue

                # Validate thesis structure before proceeding to code gen
                thesis_data = thesis_result['candidate']
                if thesis_data:
                    # A forced timeframe is authoritative — stamp it so the model
                    # can't silently fall back to daily. Batch theses are already
                    # stamped in _generate_thesis_batch; this covers the single
                    # path and is a harmless no-op if it was a batch thesis.
                    if tf_forced:
                        thesis_data['timeframe'] = tf_forced
                    else:
                        thesis_data['timeframe'] = thesis_data.get('timeframe', '').strip().upper()
                    # Same reason, same place: the academic prefix is the only
                    # attribution the category has, and this is the one point
                    # BOTH the batch and single thesis paths pass through.
                    if thesis_data.get('rationale'):
                        thesis_data['rationale'] = _canonical_academic_rationale(
                            thesis_data['rationale'])
                _thesis_err = _validate_thesis(thesis_data) if thesis_data else 'thesis is None'
                # REPAIR-ONCE (2026-08-03) instead of abandoning the iteration.
                # Adding the exit-mechanism rule to _validate_thesis turned a
                # fixable thesis into a destroyed iteration: it accounted for 2 of
                # the 5 'errors' in a 20-iteration batch, and the thesis is usually
                # sound apart from the one rejected field. The repair is targeted —
                # it hands back the SPECIFIC violation and asks for that field only,
                # so the mechanism is preserved rather than re-rolled. A shape
                # problem is repairable; 'thesis is None' is not, so that skips
                # straight to the skip path.
                if _thesis_err and thesis_data:
                    print(f"  ! Thesis invalid ({_thesis_err[:70]}) — repairing once", flush=True)
                    thesis_data = _repair_thesis_field(
                        thesis_data, _thesis_err, instrument,
                        api_key=self.api_key) or thesis_data
                    _thesis_err = _validate_thesis(thesis_data)
                if _thesis_err:
                    print(f"  ✗ Thesis validation failed: {_thesis_err}")
                    # DETERMINISTIC pre-validation skips are 'guarded', not 'errors'.
                    # 'errors' should mean a crash or an API failure — something that
                    # might be transient. A thesis the validator deliberately refused
                    # is the gate working, and counting it as an error makes the
                    # health signal unreadable (2026-08-03: 5 'errors' of which 2
                    # were deliberate exit-rule rejections).
                    _deterministic = any(k in _thesis_err for k in
                                         ('instrument2', 'bare bar count', 'too short',
                                          'unknown strategy_family', 'invalid timeframe',
                                          'param_hints'))
                    results['guarded' if _deterministic else 'errors'] += 1
                    time.sleep(self.min_delay)
                    continue
                # Rationale content-bleed guard, applied to EVERY thesis (this is
                # the chokepoint the per-iteration fallback also flows through —
                # the sub-batch parser guard misses regenerated/fallback theses).
                # Catches "rationale about Bitcoin/WTI, trades GBP_USD" before a
                # wasted self-critique call. Cross-market/pair exempt (they name a
                # second instrument legitimately).
                _fam = str(thesis_data.get('strategy_family', '')).lower()
                if not ('cross' in _fam or 'pair' in _fam or thesis_data.get('instrument2')):
                    _bleed = _rationale_instrument_mismatch(thesis_data.get('rationale', ''), instrument)
                    if _bleed:
                        print(f"  ✗ Rationale bleed: mentions {_bleed!r} but instrument is {instrument} — skipping", flush=True)
                        results['guarded'] += 1
                        time.sleep(self.min_delay)
                        continue
                strategy_family = thesis_data.get('strategy_family', 'unknown')
                rationale   = thesis_data.get('rationale', '')
                entry_cond  = thesis_data.get('entry_condition', '')
                filter_cond = thesis_data.get('filter_condition', '')
                exit_cond   = thesis_data.get('exit_condition', '')
                param_hints = thesis_data.get('param_hints', {})
                # Use timeframe from thesis if provided and valid
                thesis_tf = thesis_data.get('timeframe', '')
                if thesis_tf and thesis_tf in ('M30', 'H1', 'H4', 'D', 'W'):
                    instrument = instrument  # keep instrument
                    # will be used in code_prompt below

                if not rationale:
                    print(f"  ✗ No rationale in thesis response")
                    results['errors'] += 1
                    continue

                print(f"  Strategy Family: {strategy_family}", flush=True)
                print(f"  Rationale: {rationale[:80]}...", flush=True)
                if entry_cond:
                    print(f"  Entry:     {entry_cond[:80]}...", flush=True)
                if filter_cond:
                    print(f"  Filter:    {filter_cond[:80]}...", flush=True)
                if exit_cond:
                    print(f"  Exit:      {exit_cond[:80]}...", flush=True)

                # Step A2: Self-critique gate — reject fatal DESIGN flaws before
                # spending code-gen + validation compute. Conservative + fail-open
                # (see self_critique_thesis); enforces role-prompt design
                # discipline and never sees scores, so it can't game the validator.
                if SELF_CRITIQUE_ENABLED:
                    crit = self_critique_thesis(thesis_data, instrument, api_key=self.api_key)
                    _record_critique(thesis_data, instrument, crit)
                    if crit['verdict'] == 'reject':
                        print(f"  ✗ Self-critique rejected: {crit['reason']}", flush=True)
                        results['critiqued_out'] += 1
                        time.sleep(self.min_delay)
                        continue
                    print(f"  ✓ Self-critique passed", flush=True)

                # Step A3: Data-grounded gate — reject a thesis whose core directional
                # assumption contradicts the instrument's MEASURED in-sample structure
                # (e.g. mean-reversion on a positively-autocorrelated series). Conservative
                # (only strong contradictions) and fail-soft.
                if FINGERPRINT_ENABLED:
                    try:
                        import fingerprint
                        _fp = fingerprint.compute_fingerprint(
                            instrument, thesis_data.get('timeframe') or 'D')
                        _contra = fingerprint.contradiction(thesis_data, _fp)
                    except Exception:
                        _contra = None
                    if _contra:
                        print(f"  ✗ Contradicts measured structure: {_contra}", flush=True)
                        results['fingerprint_rejected'] += 1
                        time.sleep(self.min_delay)
                        continue

                # Step B: Generate code via OpenRouter
                print(f"  Step B: Generating code (OpenRouter)...", flush=True)

                _locked_tf = thesis_tf if (thesis_tf and thesis_tf in ('M30','H1','H4','D','W')) else 'D'
                code_prompt = _get_codegen_template().format(
                    instrument=instrument,
                    timeframe=_locked_tf,
                    family=strategy_family,
                    hypothesis=rationale,
                    entry=entry_cond if entry_cond else '(implement based on family and hypothesis)',
                    filter=filter_cond if filter_cond else 'ATR above 20-bar median (low-volatility chop filter)',
                    exit=exit_cond if exit_cond else 'Exit after 10 bars of no new signal or trailing stop',
                    param_hints=param_hints if param_hints else '{"lookback": [10, 20, 30]}',
                )

                code_result = generate_code_via_openrouter(code_prompt)

                if not code_result['success']:
                    print(f"  ✗ Code generation error: {code_result['error']}")
                    results['errors'] += 1
                    time.sleep(self.min_delay)
                    continue

                candidate = code_result['candidate']
                candidate['_model_meta'] = {**thesis_data.get('_model_meta', {}), **candidate.get('_model_meta', {})}

                # Fill in fields that the model no longer returns (we set them from thesis)
                candidate['strategy_id'] = self._generate_strategy_id(
                    instrument.lower().replace('_', ''), iteration
                )
                candidate['instrument'] = instrument
                candidate.setdefault('rationale', rationale)
                _TF_MAP = {
                    '1H': 'H1', '4H': 'H4', '1D': 'D', '1W': 'W',
                    '30M': 'M30', '30m': 'M30', '1h': 'H1', '4h': 'H4',
                    'd': 'D', 'w': 'W', 'daily': 'D', 'weekly': 'W',
                    'hourly': 'H1', '1hour': 'H1', '4hour': 'H4',
                }
                # Force timeframe to match the thesis (_locked_tf).
                # The code generator sometimes drifts (e.g. thesis says D, code returns H1)
                # which breaks strategies that use lookbacks designed for daily bars.
                code_tf_raw = candidate.get('timeframe', '')
                code_tf_norm = _TF_MAP.get(code_tf_raw, code_tf_raw)
                if code_tf_norm and code_tf_norm != _locked_tf and code_tf_norm in ('M30', 'H1', 'H4', 'D', 'W'):
                    print(f"  ↳ TF override: code returned '{code_tf_norm}' → forcing to thesis TF '{_locked_tf}'", flush=True)
                tf = _locked_tf  # authoritative: always use thesis timeframe
                candidate['timeframe'] = tf

                fidelity = post_codegen_fidelity_critique(
                    thesis_data, candidate, instrument, api_key=self.api_key)
                if fidelity['verdict'] == 'reject':
                    print(f"  ! Code/thesis mismatch: {fidelity['reason']} — regenerating once", flush=True)
                    fidelity_prompt = (
                        code_prompt
                        + "\n\nThe previous code was rejected for this thesis mismatch: "
                        + fidelity['reason']
                        + "\nRegenerate from the approved thesis. Do not invent any entry, regime gate, or exit."
                    )
                    fidelity_result = generate_code_via_openrouter(fidelity_prompt)
                    if not fidelity_result['success']:
                        print(f"  ✗ Fidelity retry error: {fidelity_result['error']}", flush=True)
                        results['errors'] += 1
                        continue
                    _saved_meta = candidate.get('_model_meta', {}).copy()
                    candidate = fidelity_result['candidate']
                    candidate['_model_meta'] = {**_saved_meta, **candidate.get('_model_meta', {}), 'repair': 'fidelity'}
                    candidate['strategy_id'] = self._generate_strategy_id(
                        instrument.lower().replace('_', ''), iteration)
                    candidate['instrument'] = instrument
                    candidate['rationale'] = rationale
                    candidate['timeframe'] = tf
                    fidelity = post_codegen_fidelity_critique(
                        thesis_data, candidate, instrument, api_key=self.api_key)
                    if fidelity['verdict'] == 'reject':
                        print(f"  ✗ Fidelity retry rejected: {fidelity['reason']}", flush=True)
                        results['errors'] += 1
                        continue

                # Normalize param_grid: some models return a list instead of dict
                raw_pg = candidate.get('param_grid', {})
                if isinstance(raw_pg, list):
                    # Try to merge list-of-dicts into a single dict
                    merged = {}
                    for item in raw_pg:
                        if isinstance(item, dict):
                            merged.update(item)
                    raw_pg = merged if merged else {}
                    candidate['param_grid'] = raw_pg
                    if raw_pg:
                        print(f"  ↳ param_grid was a list — merged into dict: {list(raw_pg.keys())}", flush=True)
                    else:
                        print(f"  ✗ param_grid is an empty/unparseable list", flush=True)
                        results['errors'] += 1
                        continue

                # Step 4: Validate candidate structure
                required = ['strategy_id', 'code', 'param_grid', 'rationale', 'timeframe']
                missing = [k for k in required if k not in candidate]
                if missing:
                    print(f"  ✗ Missing keys: {missing}")
                    results['errors'] += 1
                    continue

                # REGENERATE ONCE on a grid-shape violation (2026-08-03), mirroring
                # the fidelity retry above. This was the single most wasteful reject
                # in the funnel: 479 violations in 54 batches, each destroying a whole
                # iteration 1:1, and 362 of them were "too many params" — a limit that
                # codegen.md never stated until today. Unlike a thesis or signal
                # failure this is PURE FORMAT: the strategy may be fine and only its
                # param_grid is malformed, so telling the model the exact violation
                # and asking again is very likely to succeed. Retry ONCE only; a
                # second failure abandons the iteration exactly as before.
                grid_err = _validate_param_grid_shape(candidate['param_grid'])
                if grid_err:
                    print(f"  ! {grid_err} — regenerating once", flush=True)
                    grid_prompt = (
                        code_prompt
                        + f"\n\nThe previous code was REJECTED: {grid_err}. "
                        + "param_grid must have AT MOST 4 KEYS and AT MOST 200 total "
                        + "combinations. Keep the same strategy logic — do not change the "
                        + "entry, filter or exit. Reduce the GRID only: drop the least "
                        + "important parameters (hard-code them as constants inside "
                        + "generate_signals) and/or shorten the value lists."
                    )
                    grid_retry = generate_code_via_openrouter(grid_prompt)
                    grid_err2 = (_validate_param_grid_shape(grid_retry['candidate']['param_grid'])
                                 if grid_retry.get('success') and grid_retry.get('candidate')
                                 else 'grid retry produced no candidate')
                    if grid_err2:
                        print(f"  ✗ {grid_err2} (after retry)")
                        results['failed'].append(candidate.get('strategy_id', 'unknown'))
                        time.sleep(self.min_delay)
                        continue
                    _saved_meta = candidate.get('_model_meta', {}).copy()
                    candidate = grid_retry['candidate']
                    candidate['_model_meta'] = {**_saved_meta,
                                                **candidate.get('_model_meta', {}),
                                                'repair': 'param_grid'}
                    candidate['strategy_id'] = self._generate_strategy_id(
                        instrument.lower().replace('_', ''), iteration)
                    candidate['timeframe'] = tf
                    print(f"  ✓ grid repaired: {len(candidate['param_grid'])} params", flush=True)

                candidate['instrument'] = instrument

                # Override rationale with the approved thesis (keeps LLM honest)
                candidate['rationale'] = rationale

                # Step 5b: Validate code quality (with simple strategy enforcement)
                # Retry with SAME thesis anchored (prevents drift to new ideas)
                code_err, cleaned_code = _validate_code(candidate['code'])
                if code_err:
                    # Retry once with feedback - keep same thesis
                    print(f"  ! Code issue: {code_err}, retrying...")

                    # Extract the specific broken line for targeted feedback
                    broken_line_example = ''
                    _lnum_match = re.search(r'line (\d+):', code_err) if 'line' in code_err else None
                    if _lnum_match:
                        _lnum = int(_lnum_match.group(1)) - 1
                        _code_lines = candidate['code'].split('\n')
                        if 0 <= _lnum < len(_code_lines):
                            broken_line_example = (
                                f"\nBROKEN LINE {_lnum+1}: {_code_lines[_lnum].strip()}\n"
                                f"FIXED EXAMPLE: replace every ` and ` with ` & ` and every ` or ` with ` | `\n"
                                f"  BAD:  long_signal = long_entry and uptrend and vol_ok\n"
                                f"  GOOD: long_signal = (long_entry) & (uptrend) & (vol_ok)\n"
                            )

                    fix_prompt = f"""The previous code had this error: {code_err}
{broken_line_example}
BROKEN CODE (fix ALL occurrences of 'and'/'or' between pandas Series):
{candidate['code']}

THESIS (DO NOT CHANGE):
- Strategy Family: {strategy_family}
- Rationale: {rationale}

CRITICAL FIX REQUIRED — For every line that combines pandas Series with boolean logic:
  REPLACE every Python `and` with `&` (wrapped in parentheses)
  REPLACE every Python `or` with `|` (wrapped in parentheses)
  NEVER use Python `and`/`or` between pandas Series — it raises ValueError at runtime.

Examples:
  BAD:  entry = (rsi < 30) and (close > ema)       → ValueError
  GOOD: entry = (rsi < 30) & (close > ema)         → correct
  BAD:  sig = long_entry and uptrend and vol_ok     → ValueError
  GOOD: sig = (long_entry) & (uptrend) & (vol_ok)  → correct

Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe."""

                    fix_result = generate_code_via_openrouter(fix_prompt)
                    if fix_result['success'] and fix_result['candidate']:
                        _saved_sid = candidate.get('strategy_id')
                        _saved_meta = candidate.get('_model_meta', {}).copy()
                        candidate = fix_result['candidate']
                        candidate['_model_meta'] = {**_saved_meta, **candidate.get('_model_meta', {}), 'repair': 'code_validate'}
                        if _saved_sid:
                            candidate['strategy_id'] = _saved_sid
                        candidate['instrument'] = instrument
                        # Restore approved thesis and lock timeframe to _locked_tf
                        candidate['rationale'] = rationale
                        candidate['timeframe'] = _locked_tf  # never trust retry's TF
                        code_err, cleaned_code = _validate_code(candidate['code'])
                        if code_err:
                            print(f"  ✗ Retry failed: {code_err}")
                            results['errors'] += 1
                            continue
                        # Use cleaned code
                        candidate['code'] = cleaned_code
                    else:
                        print(f"  ✗ Retry error: {fix_result.get('error', 'failed')}")
                        results['errors'] += 1
                        continue
                else:
                    candidate['code'] = cleaned_code

                # Step 4c: Quick signal sanity check on real data.
                # Infer archetype from the code — the LLM's self-reported tag is
                # unreliable, and a mis-tagged macro strategy skips macro
                # injection and KeyErrors on us10y/fed_rate.
                candidate['archetype'] = _infer_archetype(
                    candidate['code'], candidate.get('archetype', 'standard'))
                # DEFINITIVE pair guard (code-level): the thesis text guard misses
                # cases where the model describes a pair in PROSE ('nat gas / crude
                # ratio') with a non-cross-market family, and code-gen then
                # introduces close_leg2 on its own. _infer_archetype reads the CODE,
                # so archetype=='pair' here means the code truly needs a second leg.
                # Without instrument2 it always aborts at 'No valid data' — skip and
                # regenerate instead of wasting a full validation.
                if candidate['archetype'] == 'pair' and not str(candidate.get('instrument2', '')).strip():
                    _i2 = _infer_instrument2(
                        (candidate.get('rationale', '') or '') + ' ' + (candidate.get('code', '') or ''),
                        instrument)
                    if _i2:
                        candidate['instrument2'] = _i2
                        print(f"  ↳ pair auto-assigned instrument2={_i2} (model omitted it)", flush=True)
                    else:
                        print(f"  ✗ Pair code uses close_leg2, no instrument2 and no natural "
                              f"partner for {instrument} — skipping", flush=True)
                        results['guarded'] += 1
                        time.sleep(self.min_delay)
                        continue
                # Calendar/event columns are DAY-resolution — on WEEKLY bars a
                # candle spans 5 days so ~48% of weeks contain an event and `dow`
                # is meaningless (this was the event family's 72%-weekly / 0-pass
                # bug). The forced-slot pin catches FORCED calendar/event, but a
                # SPONTANEOUS one (model chose the columns on a free W slot) reaches
                # here. Code-level catch (archetype read from the code): force daily.
                if candidate['archetype'] in ('calendar', 'news') and tf == 'W':
                    print(f"  ↳ {candidate['archetype']} archetype on weekly — day-resolution "
                          f"columns break on W bars; forcing to daily", flush=True)
                    tf = 'D'; _locked_tf = 'D'; candidate['timeframe'] = 'D'
                sig_err = _validate_basic_signals(
                    candidate['code'], candidate['param_grid'],
                    instrument=instrument, timeframe=tf,
                    archetype=candidate['archetype'],
                    instrument2=candidate.get('instrument2'),
                )
                if sig_err:
                    print(f"  ! Signal check failed: {sig_err} — retrying with looser params")
                    loose_prompt = f"""The previous strategy fired {sig_err} in 6 months of daily bars — the entry conditions are too restrictive.

THESIS (keep):
- Instrument: {instrument}
- Family: {strategy_family}
- Rationale: {rationale}
- Entry: {entry_cond}
- Filter: {filter_cond}
- Exit: {exit_cond}

BROKEN CODE (fires too rarely):
{candidate['code']}

MANDATORY FIX:
1. Make the LOOSEST param combo fire at least 15 signals in 6 months:
   - Lower any ADX threshold to 15 or less in the smallest param_grid value
   - Widen any percentile/quantile to 70th percentile or lower
   - Reduce any autocorrelation/kurtosis threshold by at least 50%
   - Reduce any rolling window by 50% in the smallest value
2. Put the LOOSEST threshold FIRST in every param_grid list
3. Never AND more than 2 conditions simultaneously in the entry signal

Output ONLY valid JSON: strategy_id, code, param_grid, rationale, timeframe."""
                    sig_fix = generate_code_via_openrouter(loose_prompt)
                    if sig_fix['success'] and sig_fix['candidate']:
                        _saved_sid = candidate.get('strategy_id')
                        _saved_meta = candidate.get('_model_meta', {}).copy()
                        # instrument2 (pair archetype) is not derivable from the
                        # code, so carry it over the regenerated candidate.
                        _saved_instrument2 = candidate.get('instrument2')
                        candidate = sig_fix['candidate']
                        candidate['_model_meta'] = {**_saved_meta, **candidate.get('_model_meta', {}), 'repair': 'signal'}
                        if _saved_sid:
                            candidate['strategy_id'] = _saved_sid
                        candidate['instrument'] = instrument
                        candidate['rationale'] = rationale
                        candidate['timeframe'] = _locked_tf
                        if _saved_instrument2 is not None:
                            candidate['instrument2'] = _saved_instrument2
                        # Re-check code quality
                        code_err2, cleaned_code2 = _validate_code(candidate['code'])
                        if code_err2:
                            print(f"  ✗ Signal retry code error: {code_err2}")
                            results['errors'] += 1
                            continue
                        candidate['code'] = cleaned_code2
                        # Re-infer archetype from the regenerated code (the loose
                        # retry doesn't ask for it; the code is authoritative).
                        candidate['archetype'] = _infer_archetype(
                            candidate['code'], candidate.get('archetype', 'standard'))
                        # Re-check signals
                        sig_err2 = _validate_basic_signals(
                            candidate['code'], candidate['param_grid'],
                            instrument=instrument, timeframe=tf,
                            archetype=candidate.get('archetype', 'standard'),
                            instrument2=candidate.get('instrument2'),
                        )
                        if sig_err2:
                            print(f"  ✗ Signal retry still failed: {sig_err2}")
                            # "only N signals" means the strategy is too
                            # restrictive to trade — that is a validation
                            # failure, not a crash. Only genuine runtime errors
                            # ("runtime error: ...") count toward the error rate.
                            if sig_err2.startswith('only '):
                                results['failed'].append(
                                    candidate.get('strategy_id', 'unknown'))
                            else:
                                results['errors'] += 1
                            continue
                        print(f"  ✓ Signal retry passed", flush=True)
                    else:
                        print(f"  ✗ Signal retry error: {sig_fix.get('error', 'failed')}")
                        results['errors'] += 1
                        continue

                # Step 5: Check fingerprint dedup
                dup_status = self._check_duplicate(candidate)
                if dup_status:
                    print(f"  ✗ Duplicate fingerprint (status: {dup_status})")
                    results['failed'].append(candidate.get('strategy_id', 'unknown'))
                    time.sleep(self.min_delay)
                    continue

                candidate['instrument'] = instrument

                print(f"  Strategy: {candidate['strategy_id']}")
                print(f"  Rationale: {candidate.get('rationale', 'none')}")

                # Step 7: Save candidate
                json_path = self._save_candidate(candidate, iteration)
                print(f"  Saved to: {json_path}")

                # Step 8: Validate
                print(f"  Validating...")
                passed, message = self._validate_candidate(candidate)

                # Query scores from DB for notification
                sid = candidate['strategy_id']

                # Tag the arm that produced this candidate. Placed AFTER validation so the
                # sidecar holds exactly the population the analysis joins to
                # validation_results — candidates that died earlier (dedup, signal errors)
                # never reach a score, so they are not part of the sample.
                _ab_tag_candidate(sid, instrument=candidate.get('instrument', ''),
                                  timeframe=candidate.get('timeframe', ''))

                db_scores = self._get_scores(sid)

                if passed:
                    results['passed'].append(sid)
                    print(f"  ✓ PASS: {message}")
                    # Notify via Telegram with Deploy/Skip buttons
                    try:
                        notify_strategy_passed(
                            strategy_id=sid,
                            instrument=candidate.get('instrument', '?'),
                            timeframe=candidate.get('timeframe', '?'),
                            rationale=candidate.get('rationale', ''),
                            is_score=db_scores.get('is_score') or 0.0,
                            wf_score=db_scores.get('wf_score') or 0.0,
                            best_params=db_scores.get('best_params') or {},
                            ho_score=db_scores.get('ho_score'),
                        )
                    except Exception as _tg_e:
                        print(f"  [Telegram] notify failed: {_tg_e}", flush=True)
                else:
                    results['failed'].append(sid)
                    print(f"  ✗ {message}")
                    # Skip per-iteration Telegram notifications

                # Rate limit
                time.sleep(self.min_delay)

            except Exception as e:
                print(f"  ❌ Iteration {iteration} crashed: {e}")
                print("  Continuing to next iteration...")
                results['errors'] += 1
                time.sleep(self.min_delay)
                continue

        # Meta-review trigger, moved here from inside the loop (2026-08-03).
        # WHY END-OF-BATCH: theses are generated UP FRONT per batch, so a directive
        # written mid-batch cannot affect the batch producing it — only the next one.
        # The old site fired on `len(results['failed']) == 15` from INSIDE the
        # validation else-branch, which broke two ways. (1) EXACT EQUALITY at ONE of
        # FOUR append sites: results['failed'] is also appended in three earlier
        # paths that never ran this check, so a batch crossing 15 via one of those
        # skipped the trigger entirely — measured 2026-08-03, it fired on only 8 of
        # 51 batches despite most of them exceeding 15 failures. (2) It silently
        # STOPPED FIRING when the failure count fell below 15: adding the exit-
        # mechanism rule to _validate_thesis meant more theses are rejected before
        # they reach validation (batch-stage validity ran 14-17 of 20), so the
        # counted failures dropped from ~19 to 10-12 and the directive loop went
        # dead with nothing to indicate it. A >= threshold checked once, after the
        # loop, cannot be skipped by an append site and does not depend on the
        # funnel's shape.
        # A chain A/B FREEZES the directives. run_meta_review() rewrites the
        # <!-- RESEARCH_PHASE --> block inside thesis.md, which _load_thesis splices into
        # every thesis prompt — so letting it fire mid-experiment changes what BOTH arms
        # are asked to produce, part-way through. It did exactly that on 2026-08-11 at
        # 13:07 (between batches 6 and 7) and contaminated the first thesis A/B: the edit
        # is never committed, so git_sha reported the run as unchanged throughout. The
        # gen_sha content hash now DETECTS it; this skip is what PREVENTS it.
        ab_frozen = bool(os.environ.get('AB_TEST_CHAIN', '').strip())
        if ab_frozen and len(results['failed']) >= META_REVIEW_MIN_FAILURES:
            print(f"\n[Meta-Review] FROZEN — AB_TEST_CHAIN is active, so the research "
                  f"directives must not move mid-experiment ({len(results['failed'])} "
                  f"failures would otherwise have triggered a rewrite).")
        elif len(results['failed']) >= META_REVIEW_MIN_FAILURES:
            print(f"\n[Meta-Review] {len(results['failed'])} failures this batch "
                  f"(>= {META_REVIEW_MIN_FAILURES}), generating new directive...")
            try:
                import meta_review
                meta_review.run_meta_review()
            except Exception as e:
                print(f"  Meta-review error: {e}")
        else:
            print(f"\n[Meta-Review] skipped — {len(results['failed'])} failures "
                  f"< {META_REVIEW_MIN_FAILURES}")

        # Final summary
        elapsed = time.time() - start
        results['duration_seconds'] = elapsed

        print(f"\n{'='*70}")
        print(f"Auto Research Complete")
        print(f"{'='*70}")
        print(f"  Iterations:     {results['iterations']}")
        print(f"  Passed:         {len(results['passed'])}")
        for pid in results['passed']:
            print(f"    ✓ {pid}")
        print(f"  Failed:         {len(results['failed'])}")
        print(f"  Self-critiqued: {results['critiqued_out']}  (design-gated pre-codegen)")
        print(f"  Struct-rejected:{results['fingerprint_rejected']}  (contradicted measured structure)")
        print(f"  Guarded:        {results['guarded']}  (deterministic pre-validation skips: bleed / pair-no-instrument2)")
        print(f"  Errors:         {results['errors']}")
        # iterations = passed + failed + self-critiqued + struct-rejected + guarded + errors
        _accounted = (len(results['passed']) + len(results['failed'])
                      + results['critiqued_out'] + results['fingerprint_rejected']
                      + results['guarded'] + results['errors'])
        if _accounted != results['iterations']:
            print(f"  (note: {results['iterations'] - _accounted} iteration(s) "
                  f"unaccounted — e.g. target-reached early stop)")
        print(f"  Duration:       {elapsed:.0f}s")
        print(f"{'='*70}\n")

        # Telegram batch-complete notification. Wrapped so a notify failure
        # (bad token / Telegram 4xx) never breaks the batch — same pattern as
        # the per-strategy notify above.
        try:
            notify_research_complete(
                iterations=results['iterations'],
                passed=results['passed'],
                failed=len(results['failed']),
                errors=results['errors'],
                duration=elapsed,
                critiqued_out=results['critiqued_out'],
                fingerprint_rejected=results['fingerprint_rejected'],
                guarded=results['guarded'],
            )
        except Exception as _tg_e:
            print(f"  [Telegram] batch-complete notify failed: {_tg_e}", flush=True)

        return results


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Auto Research: Automated strategy generation + validation loop'
    )
    parser.add_argument(
        '--target', type=int, default=3,
        help='Stop after N strategies pass validation (default: 3)'
    )
    parser.add_argument(
        '--max-iter', type=int, default=30,
        help='Maximum LLM calls before giving up (default: 30)'
    )
    parser.add_argument(
        '--instrument', type=str, default=','.join(AutoResearcher.DEFAULT_INSTRUMENT_POOL),
        help='Instrument(s) to cycle through (default: all 11 in pool). Use commas for subset, e.g. EUR_USD,XAU_USD'
    )
    parser.add_argument(
        '--model', type=str, default=DEFAULT_MODEL,
        help=f'OpenRouter model (default: {DEFAULT_MODEL})'
    )
    parser.add_argument(
        '--temperature', type=float, default=0.7,
        help='LLM temperature (default: 0.7)'
    )
    parser.add_argument(
        '--api-key', type=str, default=None,
        help='OpenRouter API key (or set OPENROUTER_API_KEY env var)'
    )
    args = parser.parse_args()

    api_key = args.api_key or OPENROUTER_API_KEY
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set. Set env var or pass --api-key.")
        sys.exit(1)

    # A/B test: alternate the whole DATA-DRIVEN generation package per batch, so a
    # DRIVEN batch (fingerprint + exploit slots) is compared against a NORMAL batch
    # (the original pure-random rotation) interleaved over the same hours/instruments.
    if os.environ.get('AB_TEST_FINGERPRINT', '0') == '1':
        global FINGERPRINT_ENABLED, EXPLOIT_ENABLED
        arm = _ab_select_fingerprint_arm()        # True = DRIVEN, False = NORMAL baseline
        FINGERPRINT_ENABLED = arm
        EXPLOIT_ENABLED = arm
        print(f"  [A/B] batch arm: "
              f"{'DRIVEN (fingerprint + exploit slots)' if arm else 'NORMAL (pure random baseline)'}",
              flush=True)

    # Chain-head A/B: swap the head of one model chain for this batch (AB_TEST_CHAIN).
    _ab_arm = _ab_chain_arm()
    if _ab_arm is not None:
        _ab_apply_arm(_ab_arm)
        # args.model defaulted to DEFAULT_MODEL when the parser was built, i.e. BEFORE the
        # swap. It is only ever printed (self.model is never called with), but a banner
        # naming the control model on a challenger batch would actively mislead the dry
        # run, so keep it truthful.
        if _ab_arm['chain'] == 'thesis':
            args.model = _ab_arm['model']

    instruments = [i.strip() for i in args.instrument.split(',')]

    ar = AutoResearcher(
        instruments=instruments,
        model=args.model,
        api_key=api_key,
        temperature=args.temperature,
    )

    results = ar.run(
        target_passed=args.target,
        max_iterations=args.max_iter,
    )

    # Exit code: 0 if target reached, 2 if exhausted iterations
    sys.exit(0 if results['passed'] else 2)


if __name__ == '__main__':
    main()
