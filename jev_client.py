"""Jev judge client for near-duplicate thesis triage (pre-codegen).

WHY. The fingerprint guard (`pipeline_utils.check_idea_is_new`) catches an
EXACT re-proposal (same code shape + param grid), and `check_signal_is_new`
catches a passed strategy that trades identically. Neither catches the
near-duplicate: a 20-bar Donchian momentum thesis has a different fingerprint
from a 25-bar Donchian momentum thesis on the same instrument and timeframe.
Jev is the probabilistic judge for that text-level gap.

SHAPE. Two Jev primitives, one HTTP round-trip:

  Noul   "is this thesis a duplicate of this reference thesis?" -> probability
  Choice "which candidate is the closest match?"  options = candidate ids + an
         explicit 'unclear' (a Choice question without an escape hatch forces a
         label onto a thesis that matches nothing).

Thresholds live in the CALLER (auto_research._jev_dup_route), never here: Jev
returns probabilities, code decides. Bands, not exact cutoffs — Jev's
probabilities vary call to call (see @vcjdeboer/jev-reliability).

FAIL-OPEN. Default is shadow: nothing is ever blocked. Any transport, parse, or
config error returns {'available': False} and the caller proceeds. This module
adds no dependency (stdlib urllib only).

WIRE CONTRACT. The exact Jev endpoint/body is deployment-specific; edit only
`_build_payload` + `_parse_answers` once the real API shape is known. `_call` is
injectable, so every routing test runs without HTTP.
"""

import json
import os
import urllib.request

JEV_ENABLED = os.getenv('JEV_ENABLED', '0') == '1'
JEV_BASE_URL = os.getenv('JEV_BASE_URL', '')
JEV_API_KEY = os.getenv('JEV_API_KEY', '')
JEV_TIMEOUT = float(os.getenv('JEV_TIMEOUT', '20'))
JEV_MODEL = os.getenv('JEV_MODEL', '')

# Shadow logs the decision and blocks nothing. Enforce applies the bands.
# Start shadow; flip only after the calibration window (see JEV_TRIAGE_LOG).
JEV_TRIAGE_MODE = os.getenv('JEV_TRIAGE_MODE', 'shadow').strip().lower()

def _build_payload(thesis_text, candidates, question_ids):
    """One Jev request: Noul per candidate + one Choice over all options.

    `question_ids` are the ids the caller will route on; the 'unclear' option is
    appended here so no caller can forget it.
    """
    questions = []
    for cid in question_ids:
        questions.append({
            'id': f'noul:{cid}',
            'kind': 'noul',
            'hypothesis': thesis_text,
            'reference': candidates[cid],
        })
    questions.append({
        'id': 'closest',
        'kind': 'choice',
        'prompt': 'Which reference thesis is the closest match to the hypothesis?',
        'hypothesis': thesis_text,
        'options': list(question_ids) + ['unclear'],
    })
    payload = {'questions': questions}
    if JEV_MODEL:
        payload['model'] = JEV_MODEL
    return payload

def _parse_answers(body, question_ids):
    """Tolerant read of the response.

    Accepts either {'answers': [...]} or a bare list. Each answer may carry its
    key in 'id'/'question_id', its probability in 'probability'/'p'. A Choice
    answer may put the picked option in 'choice'/'answer'. Missing pieces stay
    None -> caller treats the call as unavailable rather than guessing.
    """
    answers = body.get('answers') if isinstance(body, dict) else body
    if not isinstance(answers, list):
        return None
    noul = {}
    scores = {}
    closest = None
    closest_p = None
    for a in answers:
        if not isinstance(a, dict):
            continue
        qid = a.get('id') or a.get('question_id') or ''
        p = a.get('probability', a.get('p'))
        if isinstance(qid, str) and qid.startswith('noul:'):
            cid = qid[len('noul:'):]
            if isinstance(p, (int, float)):
                noul[cid] = float(p)
        elif qid == 'closest':
            picked = a.get('choice') or a.get('answer')
            if isinstance(picked, str) and picked in list(question_ids) + ['unclear']:
                closest = picked
                closest_p = float(p) if isinstance(p, (int, float)) else None
        elif isinstance(qid, str) and qid and isinstance(p, (int, float)):
            # Atomic probability question (meta-review failure modes).
            scores[qid] = float(p)
    if not noul and not scores and closest is None:
        return None
    return {'noul': noul, 'scores': scores, 'closest': closest,
            'closest_probability': closest_p}

def _http_call(payload):
    """POST one request. Raises on any failure; caller turns that into fail-open."""
    req = urllib.request.Request(
        JEV_BASE_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            **({'Authorization': f'Bearer {JEV_API_KEY}'} if JEV_API_KEY else {}),
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))

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
    if not JEV_BASE_URL:
        return {'available': False, 'noul': {}, 'closest': None,
                'closest_probability': None, 'error': 'JEV_BASE_URL unset'}
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
# Meta-review category triage: four ATOMIC questions, four independent
# probabilities. `analyze_patterns` currently collapses each failed strategy
# into ONE bucket, which conflates a broken generator with a never-firing
# signal and a genuinely dead mechanism. Jev scores the modes separately and
# the caller combines them in code (argmax + thresholds), never by parsing prose.
# ---------------------------------------------------------------------------
FAILURE_MODES = ('generator_broken', 'never_fires', 'mined_out', 'mechanism_dead')

_FAILURE_QUESTIONS = {
    'generator_broken': 'Did the generated code fail to run correctly (syntax, runtime, or data-access error) rather than trade and lose?',
    'never_fires': 'Did the strategy produce too few trades for the result to be measured (coverage failure, not performance)?',
    'mined_out': 'Did this design land close to a bar that similar prior strategies have already mined out, i.e. the edge is crowded or exhausted?',
    'mechanism_dead': 'Did the mechanism trade and then simply fail to show edge, i.e. the thesis is wrong rather than broken?',
}

def _build_failure_payload(context):
    return {'questions': [
        {'id': mode, 'kind': 'probability',
         'question': _FAILURE_QUESTIONS[mode], 'context': context}
        for mode in FAILURE_MODES]}

def judge_failure_modes(context, _call=None):
    """Four independent mode probabilities for one failed strategy.

    context: a compact dict/str (code signals, final_status, scores, reason).
    Returns {'available': bool, 'probs': {mode: float}, 'combined': mode|None,
    'error': str|None}. Never raises. `combined` is the argmax done in code.
    """
    if not JEV_ENABLED:
        return {'available': False, 'probs': {}, 'combined': None, 'error': 'jev disabled'}
    if not JEV_BASE_URL:
        return {'available': False, 'probs': {}, 'combined': None,
                'error': 'JEV_BASE_URL unset'}
    ctx = context if isinstance(context, str) else json.dumps(context)
    try:
        body = (_call or _http_call)(_build_failure_payload(ctx))
        parsed = _parse_answers(body, [])
        if not parsed or not parsed.get('scores'):
            return {'available': False, 'probs': {}, 'combined': None,
                    'error': 'unparseable response'}
        probs = {m: parsed['scores'][m] for m in FAILURE_MODES
                 if m in parsed['scores']}
        combined = max(probs, key=probs.get) if probs else None
        return {'available': True, 'probs': probs, 'combined': combined, 'error': None}
    except Exception as e:
        return {'available': False, 'probs': {}, 'combined': None,
                'error': f'{type(e).__name__}: {str(e)[:120]}'}

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
