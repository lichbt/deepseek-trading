"""Jev judge client for near-duplicate thesis triage (pre-codegen).

WHY. The fingerprint guard (`pipeline_utils.check_idea_is_new`) catches an
EXACT re-proposal (same code shape + param grid), and `check_signal_is_new`
catches a passed strategy that trades identically. Neither catches the
near-duplicate: a 20-bar Donchian momentum thesis has a different fingerprint
from a 25-bar Donchian momentum thesis on the same instrument and timeframe.
Jev is the probabilistic judge for that text-level gap.

ROUTE. OpenRouter's Decisions API (NOT chat-completions), model
`~typesafe/jev-latest` by default. One request carries several typed questions:

  Noul   "is this thesis a near-duplicate of this reference?" -> probability 0..1
  Choice "which candidate is the closest match?"  options = candidate ids + an
         explicit 'unclear' (a Choice question without an escape hatch forces a
         label onto a thesis that matches nothing).

Thresholds live in the CALLER (auto_research._jev_dup_route), never here: Jev
returns probabilities, code decides. Bands, not exact cutoffs — Jev's
probabilities vary call to call (see @vcjdeboer/jev-reliability).

FAIL-OPEN. Default is shadow: nothing is ever blocked. Any transport, parse, or
config error returns {'available': False} and the caller proceeds.

WIRE CONTRACT (OpenRouter `/api/alpha/decisions`):
  request  {model, state: {...}, questions: {id: {type, instructions, criteria}}}
  response {answers: {id: {type:'noul', noul: 0.96}
                          | {type:'choice', choice: 'x', probabilities: {...}}}}
`_call` is injectable, so every routing test runs without HTTP.
"""

import json
import os
from pathlib import Path

import requests

# Load .env HERE, not only in auto_research._load_dotenv: auto_research imports
# this module (line 31) BEFORE it calls _load_dotenv (line 50), so module-level
# config below would otherwise capture the empty shell env and JEV_ENABLED would
# read False forever. env_loader keeps existing environment variables winning,
# and a missing .env (e.g. a fresh worktree) is a no-op.
try:
    from env_loader import load_env as _load_env
    _load_env(Path(__file__).parent / '.env')
except Exception:
    pass

OPENROUTER_DECISIONS_URL = os.getenv(
    'JEV_BASE_URL', 'https://openrouter.ai/api/alpha/decisions')
JEV_ENABLED = os.getenv('JEV_ENABLED', '0') == '1'
JEV_API_KEY = os.getenv('JEV_API_KEY') or os.getenv('OPENROUTER_API_KEY', '')
JEV_MODEL = os.getenv('JEV_MODEL', '~typesafe/jev-latest')
JEV_TIMEOUT = float(os.getenv('JEV_TIMEOUT', '30'))

# Shadow logs the decision and blocks nothing. Enforce applies the bands.
# Start shadow; flip only after the calibration window (see jev_triage.jsonl).
JEV_TRIAGE_MODE = os.getenv('JEV_TRIAGE_MODE', 'shadow').strip().lower()

_Noul_TRUE = ('new_thesis restates the SAME trading idea as reference_thesis: same '
              'mechanism AND same trigger, differing only in parameters/lookback/thresholds')
_Noul_FALSE = ('new_thesis is a different trading idea, or only loosely related '
               '(shared instrument, family or vocabulary is not enough)')


def _build_payload(thesis_text, candidates, question_ids):
    """One Decisions request: a Noul per candidate + one Choice over all options.

    'unclear' is appended here so no caller can forget the escape hatch.
    """
    questions = {}
    for cid in question_ids:
        questions[f'noul:{cid}'] = {
            'type': 'noul',
            'instructions': ('Is new_thesis a near-duplicate of reference_thesis? '
                             f'reference_thesis: {candidates[cid]}'),
            'criteria': {'true': _Noul_TRUE, 'false': _Noul_FALSE},
        }
    questions['closest'] = {
        'type': 'choice',
        'instructions': ('Which single reference_thesis is the closest match to '
                         'new_thesis? Choose "unclear" if none is a real match, '
                         'even if one is loosely related.'),
        'criteria': {**{cid: candidates[cid] for cid in question_ids},
                     'unclear': 'No reference_thesis is a real match.'},
    }
    return {'model': JEV_MODEL, 'state': {'new_thesis': thesis_text},
            'questions': questions}


def _parse_answers(body, question_ids):
    """Read a Decisions response. Missing pieces stay None -> caller treats the
    call as unavailable rather than guessing."""
    if not isinstance(body, dict):
        return None
    answers = body.get('answers')
    if not isinstance(answers, dict):
        return None
    noul = {}
    scores = {}
    choices = {}
    choice_probs = {}
    closest = None
    closest_p = None
    for qid, a in answers.items():
        if not isinstance(a, dict):
            continue
        if isinstance(qid, str) and qid.startswith('noul:') and a.get('type') == 'noul':
            p = a.get('noul')
            if isinstance(p, (int, float)):
                noul[qid[len('noul:'):]] = float(p)
        elif a.get('type') == 'choice' and isinstance(qid, str):
            picked = a.get('choice')
            if isinstance(picked, str):
                choices[qid] = picked
                probs = a.get('probabilities')
                if isinstance(probs, dict):
                    choice_probs[qid] = probs
            if qid == 'closest' and picked in list(question_ids) + ['unclear']:
                closest = picked
                probs = a.get('probabilities')
                p = (probs or {}).get(picked) if isinstance(probs, dict) else None
                closest_p = float(p) if isinstance(p, (int, float)) else a.get('confidence')
        elif a.get('type') == 'noul' and isinstance(qid, str):
            p = a.get('noul')
            if isinstance(p, (int, float)):     # atomic meta-review mode question
                scores[qid] = float(p)
    if not noul and not scores and not choices and closest is None:
        return None
    return {'noul': noul, 'scores': scores, 'choices': choices,
            'choice_probs': choice_probs,
            'closest': closest, 'closest_probability': closest_p}


def _http_call(payload):
    """POST one Decisions request. Raises on failure; caller fails open."""
    resp = requests.post(
        OPENROUTER_DECISIONS_URL,
        headers={'Content-Type': 'application/json',
                 **({'Authorization': f'Bearer {JEV_API_KEY}'} if JEV_API_KEY else {})},
        json=payload, timeout=JEV_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def judge_duplicate(thesis, candidates, _call=None):
    """Ask Jev whether `thesis` duplicates any of `candidates`.

    candidates: {id: text}, shortlist from jev_candidates.shortlist (2..20 rows).
    Returns {'available': bool, 'noul': {id: float}, 'closest': id|'unclear'|None,
    'closest_probability': float|None, 'error': str|None}. Never raises.
    """
    question_ids = list(candidates or {})
    if not JEV_ENABLED:
        return {'available': False, 'noul': {}, 'closest': None,
                'closest_probability': None, 'error': 'jev disabled'}
    if len(question_ids) < 2:
        return {'available': False, 'noul': {}, 'closest': None,
                'closest_probability': None, 'error': 'no candidate pool'}
    if not JEV_API_KEY:
        return {'available': False, 'noul': {}, 'closest': None,
                'closest_probability': None, 'error': 'OPENROUTER_API_KEY unset'}
    thesis_text = thesis if isinstance(thesis, str) else json.dumps(thesis)
    try:
        body = (_call or _http_call)(_build_payload(thesis_text, candidates, question_ids))
        parsed = _parse_answers(body, question_ids)
        if not parsed:
            return {'available': False, 'noul': {}, 'closest': None,
                    'closest_probability': None, 'error': 'unparseable response'}
        return {'available': True, 'error': None, **parsed}
    except Exception as e:
        return {'available': False, 'noul': {}, 'closest': None,
                'closest_probability': None, 'error': f'{type(e).__name__}: {str(e)[:120]}'}


# ---------------------------------------------------------------------------
# Thesis triage: four atomic defect questions + one Choice, per thesis, asked
# BEFORE codegen. The existing self-critique returns a binary reject/pass with a
# prose reason; these are independent probabilities (no parsing) covering the
# four defect types, and `dominant_defect` names the single worst one in code.
# ---------------------------------------------------------------------------
THESIS_DEFECT_MODES = ('fidelity', 'regime', 'mechanism', 'density')

_THESIS_DEFECT_QUESTIONS = {
    'filter_independent': ('Does the filter measure a DIFFERENT quantity than the entry, '
                           'so it can be false while the entry is true?'),
    'exit_tied_to_entry': ("Does the exit reference the entry's own quantity "
                           '(same level/price/indicator), rather than an independent rule)?'),
    'direction_stated': ('Does the entry state LONG or SHORT explicitly, with no '
                         'ambiguous or missing side?'),
    'density_risk': ('Is this at high risk of too few trades to score — extreme '
                     'rarity (session-only, multi-condition, <=1/yr-style) rather '
                     'than a normal filter?'),
    # POSITIVE check (true = good): the mechanism counterparty is named. Without
    # it a 'mechanism' defect could only be repaired on the diffuse Choice
    # confidence, so flagged theses went unrepaired (observed 2026-10-03).
    'mechanism_named': ('Does the rationale name a SPECIFIC economic counterparty '
                        'or flow (who is on the other side, e.g. month-end pension '
                        'inflows, exporter hedging, CTA trend-following) AND why '
                        'that flow moves price? A generic state description '
                        '(e.g. "thin liquidity", "high volatility") is not enough.'),
}

_DOMINANT_DEFECT_OPTIONS = {
    'fidelity': 'Entry/exit logic contradicts the stated mechanism.',
    'regime': 'The regime gate adds no state the entry did not already require.',
    'mechanism': 'The rationale names no real economic driver.',
    'density': 'The design will not produce enough trades to score.',
    'NONE': 'No defect above is present.',
    'OTHER': 'A defect not covered by these categories.',
}


def _build_thesis_defect_payload(thesis_text):
    questions = {}
    for qid, ask in _THESIS_DEFECT_QUESTIONS.items():
        questions[qid] = {
            'type': 'noul',
            'instructions': ask,
            'criteria': {'true': f'Yes. {ask}', 'false': 'No, the thesis does not have this defect.'},
        }
    questions['dominant_defect'] = {
        'type': 'choice',
        'instructions': ('Which single defect, if any, is dominant in new_thesis? '
                         'Choose NONE if none is present, OTHER if none of these fit.'),
        'criteria': dict(_DOMINANT_DEFECT_OPTIONS),
    }
    return {'model': JEV_MODEL, 'state': {'new_thesis': thesis_text},
            'questions': questions}


def defect_strengths(probs):
    """Per-defect-type strength from the four Noul answers, polarity handled in code.

    The Nouls are not all defect-shaped: `filter_independent` and `direction_stated`
    are POSITIVE checks (high = good), while `exit_tied_to_entry` and
    `density_risk` are defect checks (high = bad). Averaging them would be wrong,
    and the Choice confidence alone is often diffuse, so each defect type gets an
    explicit rule here. 'mechanism' comes from the inverse of `mechanism_named`; the Choice
    label is only a fallback when that Noul is missing.
    """
    p = probs or {}

    def g(k):
        v = p.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    fi, et, ds, dr = g('filter_independent'), g('exit_tied_to_entry'), g('direction_stated'), g('density_risk')
    mn = g('mechanism_named')
    return {
        'fidelity': max([v for v in (et, None if ds is None else 1.0 - ds) if v is not None],
                        default=None),
        'regime': None if fi is None else 1.0 - fi,
        'density': dr,
        'mechanism': None if mn is None else 1.0 - mn,
    }


def judge_thesis_defects(thesis, _call=None):
    """Four independent defect probabilities + one dominant-defect Choice.

    Returns {'available', 'probs': {fidelity,regime,mechanism,density via the
    4 Noul ids}, 'dominant_defect': str|None, 'choices': {...}, 'error'}.
    The caller maps Noul ids to defect types (see THESIS_DEFECT_IDS_TO_TYPES).
    Never raises.
    """
    if not JEV_ENABLED:
        return {'available': False, 'probs': {}, 'dominant_defect': None,
                'dominant_probability': None, 'choices': {}, 'error': 'jev disabled'}
    if not JEV_API_KEY:
        return {'available': False, 'probs': {}, 'dominant_defect': None,
                'dominant_probability': None, 'choices': {},
                'error': 'OPENROUTER_API_KEY unset'}
    thesis_text = thesis if isinstance(thesis, str) else json.dumps(thesis)
    try:
        body = (_call or _http_call)(_build_thesis_defect_payload(thesis_text))
        parsed = _parse_answers(body, [])
        if not parsed:
            return {'available': False, 'probs': {}, 'dominant_defect': None,
                    'dominant_probability': None, 'choices': {},
                    'error': 'unparseable response'}
        choices = parsed.get('choices') or {}
        dom = choices.get('dominant_defect')
        dom_p = ((parsed.get('choice_probs') or {}).get('dominant_defect') or {}).get(dom)
        probs = parsed.get('scores') or {}
        return {'available': True, 'error': None,
                'probs': probs,
                'strengths': defect_strengths(probs),
                'choices': choices,
                'dominant_defect': dom,
                'dominant_probability': dom_p}
    except Exception as e:
        return {'available': False, 'probs': {}, 'dominant_defect': None,
                'dominant_probability': None, 'choices': {},
                'error': f'{type(e).__name__}: {str(e)[:120]}'}


# Noul id -> defect type, for reference only. NOTE POLARITY: filter_independent
# and direction_stated are POSITIVE checks (high = good), while exit_tied_to_entry
# and density_risk are DEFECT checks (high = bad). Do not average them into one
# score. `dominant_defect` is the authoritative label; code routes on that.
THESIS_DEFECT_IDS_TO_TYPES = {
    'filter_independent': 'regime',
    'exit_tied_to_entry': 'fidelity',
    'direction_stated': 'fidelity',
    'density_risk': 'density',
    'mechanism_named': 'mechanism',
}


# ---------------------------------------------------------------------------
# Meta-review category triage: four ATOMIC questions, four independent
# probabilities. `analyze_patterns` currently collapses each failed strategy
# into ONE bucket, which conflates a broken generator with a never-firing
# signal and a genuinely dead mechanism. Jev scores the modes separately and
# the caller combines them in code (argmax + thresholds), never by parsing prose.
# ---------------------------------------------------------------------------
FAILURE_MODES = ('generator_broken', 'never_fires', 'mined_out', 'mechanism_dead')

_FAILURE_QUESTIONS = {
    'generator_broken': 'Is the generator structurally broken (reject rate >30%)?',
    'never_fires': 'Do theses fail to produce a scoreable sample (too few trades to measure)?',
    'mined_out': 'Is conversion declining across successive batches?',
    'mechanism_dead': 'Is the mechanism absent, rather than the generator at fault?',
}

_RECOMMENDED_ACTION_OPTIONS = {
    'fix_prompt': 'Fix the generation prompt / role guidance.',
    'fix_density': 'Fix trade density (loosen filters, increase activity).',
    'maintain': 'Keep the current configuration as-is.',
    'retire': 'Retire the current direction / stop pursuing it.',
    'OTHER': 'A different action not listed above.',
}


def _build_failure_payload(context):
    questions = {}
    for mode in FAILURE_MODES:
        questions[mode] = {
            'type': 'noul',
            'instructions': _FAILURE_QUESTIONS[mode],
            'criteria': {'true': f'Yes. {_FAILURE_QUESTIONS[mode]}',
                         'false': 'No, this does not describe the batch.'},
        }
    questions['recommended_action'] = {
        'type': 'choice',
        'instructions': ('What single action does the batch evidence recommend? '
                         'Choose OTHER if none of these fit.'),
        'criteria': dict(_RECOMMENDED_ACTION_OPTIONS),
    }
    return {'model': JEV_MODEL, 'state': {'batch_failure_context': context},
            'questions': questions}


def judge_failure_modes(context, _call=None):
    """Four independent mode probabilities for one failed strategy.

    context: a compact dict/str (code signals, final_status, scores, reason).
    Returns {'available': bool, 'probs': {mode: float}, 'combined': mode|None,
    'error': str|None}. Never raises. `combined` is the argmax done in code.
    """
    if not JEV_ENABLED:
        return {'available': False, 'probs': {}, 'combined': None, 'recommended_action': None, 'error': 'jev disabled'}
    if not JEV_API_KEY:
        return {'available': False, 'probs': {}, 'combined': None,
                'recommended_action': None, 'error': 'OPENROUTER_API_KEY unset'}
    ctx = context if isinstance(context, str) else json.dumps(context)
    try:
        body = (_call or _http_call)(_build_failure_payload(ctx))
        parsed = _parse_answers(body, [])
        if not parsed or not parsed.get('scores'):
            return {'available': False, 'probs': {}, 'combined': None,
                    'recommended_action': None, 'error': 'unparseable response'}
        probs = {m: parsed['scores'][m] for m in FAILURE_MODES
                 if m in parsed['scores']}
        combined = max(probs, key=probs.get) if probs else None
        action = (parsed.get('choices') or {}).get('recommended_action')
        return {'available': True, 'probs': probs, 'combined': combined,
                'recommended_action': action, 'error': None}
    except Exception as e:
        return {'available': False, 'probs': {}, 'combined': None,
                'recommended_action': None, 'error': f'{type(e).__name__}: {str(e)[:120]}'}


def summarize_shadow_log(path=None):
    """One-screen summary of the shadow log; calibration aid, no dependencies.

    Prints availability, route counts, and the named-match probability spread so
    the 0.45/0.70 bands can be moved on evidence. Call-to-call variance is a
    separate question (@vcjdeboer/jev-reliability); this reads only what the
    pipeline recorded.
    """
    import collections
    p = path or os.getenv('JEV_TRIAGE_LOG', '')
    rows = []
    try:
        with open(p) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        return f'no log at {p}'
    if not rows:
        return 'log empty'
    n = len(rows)
    avail = sum(1 for r in rows if r.get('available'))
    routes = dict(collections.Counter(r.get('route') for r in rows))
    out = [f'rows={n}  available={avail} ({avail * 100 // n}%)  mode={rows[-1].get("mode")}',
           f'routes: {routes}']
    named = [r for r in rows if r.get('closest') not in (None, 'unclear')]
    if named:
        ps = sorted(float(r.get('probability') or 0) for r in named)
        above = lambda x: sum(1 for v in ps if v >= x)
        out.append(f'named={len(ps)}  >=0.45={above(0.45)}  >=0.70={above(0.70)}')
        out.append(f'  min={ps[0]:.2f} median={ps[len(ps) // 2]:.2f} max={ps[-1]:.2f}')
    else:
        out.append('no named matches yet')
    return '\n'.join(out)
