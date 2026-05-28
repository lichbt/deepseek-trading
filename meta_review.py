#!/usr/bin/env python3
"""
Meta-Review: LLM-powered failure pattern analysis + research directive generation.
Falls back to rule-based if LLM fails.

Usage:
    python meta_review.py
"""

import sqlite3
import os
import json
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

DB_PATH = Path(__file__).parent / 'pipeline.db'
PROGRAM_MD = Path(__file__).parent / 'program.md'
THESIS_MD  = Path(__file__).parent / 'thesis.md'
REVIEWER_MD = Path(__file__).parent / 'reviewer.md'
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
OPENROUTER_BASE = 'https://openrouter.ai/api/v1'

# Fallback rule-based thresholds
SILENCE_THRESHOLD = 0.6   # if >=60% fail with WF=0
LOW_IS_THRESHOLD = 0.6    # if >=60% have IS < 0.1
DECAY_THRESHOLD = 0.4     # if >=40% have holdout decay

# LLM prompt template for directive generation
LLM_USER_TEMPLATE = """Analyze recent validation failures and generate new research directives.

Recent Stats:
- Total: {total}
- Passed: {passed_count}
- Avg IS: {avg_is:.4f}
- Avg WF: {avg_wf:.4f}
- Regime silence: {regime_silence}
- Low IS: {low_is}
- Holdout decay: {decay}

Gate failure breakdown:
{gate_breakdown}

Timeframe breakdown:
{tf_breakdown}

Instrument breakdown:
{inst_breakdown}

Recent failed rationales:
{failed_rationales}

Current directive:
{current_directive}

Generate 3 new bullet points (under 100 chars each) for research focus."""


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_recent_results(limit: int = 30) -> List[Dict]:
    """Fetch recent validation results from DB."""
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute('''
        SELECT v.strategy_id, v.final_status, v.is_gt_score,
               v.walk_forward_gt_score, v.holdout_gt_score, v.tested_at,
               s.rationale, s.code, s.param_grid, s.timeframe
        FROM validation_results v
        JOIN strategies s ON s.id = v.strategy_id
        ORDER BY v.tested_at DESC
        LIMIT ?
    ''', (limit,))
    rows = cur.fetchall()
    conn.close()

    # Strategies table has no instrument column — derive it from strategy_id prefix
    out = []
    for r in rows:
        d = dict(r)
        d['instrument'] = _infer_instrument_from_id(d.get('strategy_id', ''))
        out.append(d)
    return out


# Known instrument prefixes used by auto-generated IDs (e.g. "gbpusd_auto_...")
# and by manually named strategies (e.g. "xau_usd_volatility_v1").
_INSTRUMENT_PREFIXES = [
    'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF', 'AUD_USD', 'NZD_USD',
    'GBP_JPY', 'EUR_JPY', 'EUR_GBP', 'XAU_USD', 'XAG_USD', 'BCO_USD',
    'BTC_USD', 'ETH_USD', 'LTC_USD', 'WTICO_USD', 'NATGAS_USD',
    'CORN_USD', 'SOYBN_USD', 'WHEAT_USD',
]


def _infer_instrument_from_id(strategy_id: str) -> str:
    """Best-effort instrument inference from strategy_id naming convention."""
    if not strategy_id:
        return 'unknown'
    sid = strategy_id.lower()
    # Compact form: "gbpusd_auto_..." or "gbpusd_v1"
    compact = sid.split('_auto_')[0].replace('_', '')
    for inst in _INSTRUMENT_PREFIXES:
        if compact.startswith(inst.lower().replace('_', '')):
            return inst
    # Expanded form: "xau_usd_volatility_v1" → starts with "xau_usd"
    for inst in _INSTRUMENT_PREFIXES:
        if sid.startswith(inst.lower() + '_'):
            return inst
    return 'unknown'


def get_current_program_md() -> str:
    """Read current research directives (from thesis.md markers, fallback to program.md)."""
    for path in (THESIS_MD, PROGRAM_MD):
        if path.exists():
            text = path.read_text()
            if '<!-- RESEARCH_PHASE_START -->' in text:
                return text
    return ''


def get_reviewer_system_prompt() -> str:
    """Read the reviewer.md system prompt file."""
    if not REVIEWER_MD.exists():
        print(f'  WARNING: {REVIEWER_MD} not found, using embedded prompt')
        return _get_embedded_system_prompt()
    return REVIEWER_MD.read_text()


def _get_embedded_system_prompt() -> str:
    """Fallback embedded system prompt (when reviewer.md is missing)."""
    return """You are a quantitative trading strategy research analyst. Your job is to analyze failure patterns
from recent backtest results and generate actionable research directives.

Generate exactly 3 bullet points (under 100 chars each) that become new research directives.
Rules:
- Each bullet must be ACTIONABLE and SPECIFIC
- Focus on what is different from the failed patterns
- Suggest specific indicator combinations, timeframe changes, or archetype switches
- Do NOT repeat directives already in current program.md
- CRITICAL: No volume, COT, order book, or sentiment. Only OHLC data.
- CRITICAL: Timeframes allowed are M30, H1, H4, D, W only.
- Output ONLY the 3 bullets, no explanation, no preamble, each starting with "- " """


def extract_current_directive() -> Optional[str]:
    """Get what's currently in the RESEARCH_PHASE section."""
    content = get_current_program_md()
    if not content:
        return None

    start = content.find('<!-- RESEARCH_PHASE_START -->')
    end = content.find('<!-- RESEARCH_PHASE_END -->')
    if start == -1 or end == -1:
        return None

    # Extract the content between markers (but not the markers themselves)
    start += len('<!-- RESEARCH_PHASE_START -->')
    section = content[start:end].strip()
    return section if section else None


# ============================================================================
# PATTERN ANALYSIS (rule-based, used for fallback + LLM context)
# ============================================================================

def analyze_patterns(results: List[Dict]) -> Dict:
    """Rule-based pattern analysis. Used both as LLM context and fallback."""
    if not results:
        return {'total': 0, 'message': 'No results yet'}

    passed = [r for r in results if 'pass' in (r.get('final_status') or '').lower()]
    failed = [r for r in results if 'fail' in (r.get('final_status') or '').lower()]

    statuses = [r.get('final_status', '') for r in results]
    is_scores = [r.get('is_gt_score') for r in results if r.get('is_gt_score') is not None]
    wf_scores = [r.get('walk_forward_gt_score') for r in results if r.get('walk_forward_gt_score') is not None]

    regime_silence = sum(1 for s in statuses if 'WF 0' in s or s == 'FAIL: Validation did not pass all gates')
    low_is = sum(1 for s in is_scores if s is not None and s < 0.1)
    decay = sum(1 for r in failed if 'decay' in (r.get('final_status') or '').lower())
    no_wf_trades = sum(1 for wf in wf_scores if wf is not None and wf == 0.0)

    # Failure reason counts by gate
    gate_counts = {
        'duplicate': 0,
        'code': 0,
        'data': 0,
        'is': 0,
        'wf': 0,
        'sparse': 0,
        'holdout': 0,
        'other': 0,
    }

    inst_stats = {}
    tf_stats = {}

    for r in results:
        status = (r.get('final_status') or '').lower()
        timeframe = r.get('timeframe', 'D')
        instrument = r.get('instrument', 'unknown')
        passed_flag = 'pass' in status

        if instrument not in inst_stats:
            inst_stats[instrument] = {'total': 0, 'passed': 0, 'failed': 0, 'avg_is': []}
        inst_stats[instrument]['total'] += 1
        inst_stats[instrument]['passed' if passed_flag else 'failed'] += 1
        if r.get('is_gt_score') is not None:
            inst_stats[instrument]['avg_is'].append(r['is_gt_score'])

        if timeframe not in tf_stats:
            tf_stats[timeframe] = {'total': 0, 'passed': 0, 'failed': 0, 'wf_zeros': 0}
        tf_stats[timeframe]['total'] += 1
        tf_stats[timeframe]['passed' if passed_flag else 'failed'] += 1
        if r.get('walk_forward_gt_score') == 0.0:
            tf_stats[timeframe]['wf_zeros'] += 1

        if not passed_flag:
            if 'duplicate' in status:
                gate_counts['duplicate'] += 1
            elif 'code error' in status or 'syntax' in status:
                gate_counts['code'] += 1
            elif 'data' in status or 'candles' in status:
                gate_counts['data'] += 1
            elif status.startswith('fail: is') or ' is ' in status:
                gate_counts['is'] += 1
            elif 'sparse trades' in status:
                gate_counts['sparse'] += 1
            elif 'holdout' in status or 'decay' in status:
                gate_counts['holdout'] += 1
            elif 'wf' in status or 'walk forward' in status:
                gate_counts['wf'] += 1
            else:
                gate_counts['other'] += 1

    return {
        'total': len(results),
        'passed_count': len(passed),
        'failed_count': len(failed),
        'avg_is': round(sum(is_scores) / len(is_scores), 4) if is_scores else 0,
        'avg_wf': round(sum(wf_scores) / len(wf_scores), 4) if wf_scores else 0,
        'regime_silence': regime_silence,
        'low_is': low_is,
        'decay': decay,
        'no_wf_trades': no_wf_trades,
        'gate_counts': gate_counts,
        'inst_stats': inst_stats,
        'tf_stats': tf_stats,
        'recent_rationales': [r.get('rationale', '') for r in failed[:10] if r.get('rationale')],
    }


# ============================================================================
# LLM DIRECTIVE GENERATION
# ============================================================================

# Meta-review LLM (OpenRouter). Returns RAW TEXT (directive bullets, or
# PROPOSE/NO_CHANGE), not JSON. Meta-review runs rarely (only on a run of
# consecutive failures), so the paid DeepSeek V4 is used as primary for
# stronger failure-pattern reasoning and prompt-revision quality; a free
# model backs it up if the paid call ever fails.
# Note: OpenRouter has no "deepseek-v4-pro" slug — paid V4 is deepseek-v4-flash.
META_MODEL = 'deepseek/deepseek-v4-flash'           # paid (no :free) — reliable, no rate limit
META_MODEL_FALLBACK = 'openai/gpt-oss-120b:free'    # free backstop


def call_llm(system_prompt: str, user_prompt: str, model: str = None) -> Optional[str]:
    """
    Call OpenRouter for meta-review text generation. Returns the model's raw
    text output, or None on any failure (callers fall back to rule-based).

    Tries META_MODEL then META_MODEL_FALLBACK unless an explicit model is given.
    """
    if not OPENROUTER_API_KEY:
        print('  LLM: no OPENROUTER_API_KEY — skipping LLM')
        return None

    # Load reviewer system prompt if using the sentinel
    if system_prompt == 'REVIEWER_PROMPT':
        system_prompt = get_reviewer_system_prompt()

    models = [model] if model else [META_MODEL, META_MODEL_FALLBACK]
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json',
    }

    for m in models:
        payload = {
            'model': m,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'temperature': 0.7,
            'max_tokens': 1200,
        }
        try:
            resp = requests.post(
                f'{OPENROUTER_BASE}/chat/completions',
                headers=headers, json=payload, timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            if 'error' in data and 'choices' not in data:
                err = data.get('error')
                msg = err.get('message') if isinstance(err, dict) else str(err)
                print(f'  LLM: {m} error — {str(msg)[:150]}')
                continue
            content = data['choices'][0]['message'].get('content')
            if content:
                return content.strip()
            finish = data['choices'][0].get('finish_reason', 'unknown')
            print(f'  LLM: {m} empty content (finish_reason={finish})')
        except Exception as e:
            print(f'  LLM: {m} failed — {str(e)[:150]}')
            continue

    return None


def _build_llm_prompt(analysis: Dict, current_directive: Optional[str]) -> str:
    """Build the LLM prompt from analysis data."""
    total = analysis.get('total', 0)

    # Timeframe breakdown
    tf_lines = []
    for tf, stats in analysis.get('tf_stats', {}).items():
        pass_rate = f"{stats['passed']}/{stats['total']}" if stats['total'] else '0/0'
        pct_zeros = stats['wf_zeros'] / stats['total'] if stats['total'] else 0
        tf_lines.append(f"  {tf}: {pass_rate} pass, {pct_zeros:.0%} WF=0")
    tf_breakdown = '\n'.join(tf_lines) or '  (none)'

    # Instrument breakdown
    inst_lines = []
    for inst, stats in analysis.get('inst_stats', {}).items():
        pass_rate = f"{stats['passed']}/{stats['total']}" if stats['total'] else '0/0'
        avg_is = sum(stats['avg_is']) / len(stats['avg_is']) if stats['avg_is'] else 0
        inst_lines.append(f"  {inst}: {pass_rate} pass, avg IS={avg_is:.4f}")
    inst_breakdown = '\n'.join(inst_lines) or '  (none)'

    # Gate failure counts
    gate_counts = analysis.get('gate_counts', {})
    gate_lines = [f"  {k}: {v}" for k, v in sorted(gate_counts.items()) if v > 0]
    gate_breakdown = '\n'.join(gate_lines) or '  (none)'

    # Failed rationales
    failed_rationales = '\n'.join(
        f"  - {r}" for r in analysis.get('recent_rationales', [])[:5]
    ) or '  (none)'

    current = current_directive or '(none — fresh start)'

    return LLM_USER_TEMPLATE.format(
        total=total,
        passed_count=analysis.get('passed_count', 0),
        avg_is=analysis.get('avg_is', 0),
        avg_wf=analysis.get('avg_wf', 0),
        regime_silence=analysis.get('regime_silence', 0),
        low_is=analysis.get('low_is', 0),
        decay=analysis.get('decay', 0),
        gate_breakdown=gate_breakdown,
        tf_breakdown=tf_breakdown,
        inst_breakdown=inst_breakdown,
        failed_rationales=failed_rationales,
        current_directive=current,
    )


# ============================================================================
# FALLBACK: RULE-BASED DIRECTIVE
# ============================================================================

def generate_rule_based_directive(analysis: Dict) -> str:
    """Rule-based fallback when LLM is unavailable or fails."""
    if 'error' in analysis or 'message' in analysis:
        return "- All archetypes allowed.\n- Try mean-reversion on EUR/USD with RSI.\n- Explore H4 timeframe."

    total = analysis.get('total', 0)
    if total == 0:
        return "- All archetypes allowed.\n- Try RSI-based mean reversion on EUR/USD.\n- Explore H4 timeframe."

    gate_counts = analysis.get('gate_counts', {})
    silence_pct = analysis['regime_silence'] / total if total else 0
    low_is_pct = analysis['low_is'] / total if total else 0
    decay_pct = analysis['decay'] / total if total else 0

    lines = []

    # Use gate failure counts to steer directive
    top_gates = sorted(gate_counts.items(), key=lambda x: -x[1])
    if top_gates and top_gates[0][1] > 0:
        top_gate, top_count = top_gates[0]
        if top_gate == 'sparse' or top_gate == 'wf':
            lines.append(f"- Sparse trade failures dominant ({top_count}/{total}). Add volatility filter (ATR) and explicit exit rules to increase activity.")
        elif top_gate == 'holdout':
            lines.append(f"- Holdout decay dominant ({top_count}/{total}). Prefer mean-reversion over trend-following for better generalization.")
        elif top_gate == 'is':
            lines.append(f"- In-sample failures dominant ({top_count}/{total}). Simplify param grids to 2-3 key params, avoid overfitting.")
        elif top_gate == 'code':
            lines.append(f"- Code/syntax errors dominant ({top_count}/{total}). Ensure robust signal generation with proper error handling.")
        elif top_gate == 'duplicate':
            lines.append(f"- Duplicate fingerprints dominant ({top_count}/{total}). Try different instruments or param ranges.")
        else:
            if silence_pct >= SILENCE_THRESHOLD:
                lines.append(f"- Regime silence dominant ({analysis['regime_silence']}/{total}). Switch to H4 timeframe for more trading opportunities.")
            elif low_is_pct >= LOW_IS_THRESHOLD:
                lines.append(f"- Low IS scores ({analysis['low_is']}/{total}). Use simpler models with fewer parameters.")
            elif decay_pct >= DECAY_THRESHOLD:
                lines.append(f"- Holdout decay ({analysis['decay']}/{total}). Prefer mean-reversion strategies over trend-following.")
    else:
        if silence_pct >= SILENCE_THRESHOLD:
            lines.append(f"- Regime silence dominant ({analysis['regime_silence']}/{total}). Use shorter timeframes or add volatility filters.")
        elif low_is_pct >= LOW_IS_THRESHOLD:
            lines.append(f"- Low IS scores ({analysis['low_is']}/{total}). Simplify indicator combinations.")
        elif decay_pct >= DECAY_THRESHOLD:
            lines.append(f"- Holdout decay ({analysis['decay']}/{total}). Prefer mean-reversion strategies.")

    # WF score guidance
    avg_wf = analysis.get('avg_wf', 0)
    if avg_wf < 0.05:
        lines.append(f"- Avg WF score {avg_wf:.4f} very low; try strategies that trade more frequently (every 5-15 bars).")

    if not lines:
        lines.append("- All archetypes allowed; try mean-reversion on EUR/USD or carry-trade on GBP/JPY.")

    return '\n'.join(lines)


# ============================================================================
# UPDATE THESIS.MD (primary) — falls back to program.md if markers not found
# ============================================================================

def update_research_phase(directive: str) -> bool:
    """Replace the RESEARCH_PHASE section in thesis.md (primary) or program.md (fallback)."""
    start_marker = '<!-- RESEARCH_PHASE_START -->'
    end_marker   = '<!-- RESEARCH_PHASE_END -->'

    # Prefer thesis.md; fall back to program.md for backward compat
    target = None
    for path in (THESIS_MD, PROGRAM_MD):
        if path.exists() and start_marker in path.read_text():
            target = path
            break

    if target is None:
        print('ERROR: RESEARCH_PHASE markers not found in thesis.md or program.md')
        return False

    content  = target.read_text()
    start_idx = content.find(start_marker) + len(start_marker)
    end_idx   = content.find(end_marker)

    new_content = (
        content[:start_idx]
        + '\n' + directive.strip() + '\n'
        + content[end_idx:]
    )

    target.write_text(new_content)
    print(f'  ✓ Research phase written to {target.name}')
    return True


# ============================================================================
# ROLE-PROMPT REVISION (propose-only; human approves before apply)
# ============================================================================
# Unlike research directives (small, bounded, auto-applied to a delimited
# block), the Role section is the stable identity/contract of the thesis
# prompt. meta_review may PROPOSE a revision when a systematic blind spot
# dominates a batch, but it NEVER edits the Role section itself — it writes a
# proposal file + Telegram summary, and a human approves before apply.

ROLE_START_MARKER = '<!-- ROLE_START -->'
ROLE_END_MARKER   = '<!-- ROLE_END -->'
ROLE_PROPOSALS_DIR = Path(__file__).parent / '.role-proposals'
# Fire only when one failure stage dominates this fraction of failures.
ROLE_PATTERN_DOMINANCE = 0.70
# Don't propose more than once per this many hours (rate limit).
ROLE_PROPOSAL_COOLDOWN_HOURS = 24


def extract_current_role() -> Optional[str]:
    """Read the current Role-section text between the ROLE markers in thesis.md."""
    if not THESIS_MD.exists():
        return None
    content = THESIS_MD.read_text()
    s = content.find(ROLE_START_MARKER)
    e = content.find(ROLE_END_MARKER)
    if s == -1 or e == -1:
        return None
    s += len(ROLE_START_MARKER)
    section = content[s:e].strip()
    return section or None


def _dominant_failure_pattern(analysis: Dict) -> Optional[Dict]:
    """
    Detect whether a SINGLE failure stage dominates the batch. Returns a dict
    {stage, count, total, fraction} if one stage is >= ROLE_PATTERN_DOMINANCE
    of all failures, else None. This is the trigger gate for a Role proposal —
    a Role-level steer is only warranted when the pool shares one structural
    flaw, not when failures are diffuse.
    """
    gate_counts = analysis.get('gate_counts', {}) or {}
    # Stages that reflect an *idea-quality* blind spot the Role wording could
    # plausibly influence. Code/data/duplicate failures are plumbing, not the
    # Role's job — exclude them so we don't propose prompt edits for bugs.
    idea_stages = {'is', 'wf', 'sparse', 'holdout', 'other'}
    relevant = {k: v for k, v in gate_counts.items() if k in idea_stages and v > 0}
    total = sum(relevant.values())
    if total < 5:  # too little signal to justify a core-prompt change
        return None
    stage, count = max(relevant.items(), key=lambda kv: kv[1])
    frac = count / total
    if frac < ROLE_PATTERN_DOMINANCE:
        return None
    return {'stage': stage, 'count': count, 'total': total, 'fraction': round(frac, 3)}


def _role_proposal_on_cooldown() -> bool:
    """True if a proposal was written within the cooldown window."""
    if not ROLE_PROPOSALS_DIR.exists():
        return False
    proposals = sorted(ROLE_PROPOSALS_DIR.glob('role_proposal_*.md'))
    if not proposals:
        return False
    newest = proposals[-1]
    age_h = (datetime.now().timestamp() - newest.stat().st_mtime) / 3600.0
    return age_h < ROLE_PROPOSAL_COOLDOWN_HOURS


_ROLE_PROPOSAL_PROMPT = """You are reviewing the ROLE section of a thesis-generation prompt for a \
systematic-trading research pipeline. The Role section is the stable identity/contract that \
steers an LLM to produce trading-strategy theses.

A batch of recently generated strategies failed validation with a DOMINANT pattern:
  - Dominant failure stage: {stage} ({count}/{total} = {pct}% of idea-quality failures)
  - Avg in-sample score: {avg_is}
  - Avg walk-forward score: {avg_wf}
  - Sample failing rationales:
{rationales}

CURRENT ROLE SECTION:
\"\"\"
{current_role}
\"\"\"

Decide whether this dominant failure pattern reflects a SYSTEMATIC BLIND SPOT in the Role \
wording that a revised Role could address — for example, the pool keeps producing the same \
structural flaw (all unconditional mean-reversion, all overfit-prone, all generic FX framing on \
commodities, etc.).

If a Role revision is warranted, output EXACTLY:
PROPOSE
<the full revised Role section text>

If the pattern does NOT warrant a core-prompt change (e.g. it's just normal rejection of \
edgeless ideas, or a plumbing issue), output EXACTLY:
NO_CHANGE

Rules for any proposed Role text:
- Keep it the same general length and shape as the current Role (2 short paragraphs).
- Do NOT add specific numeric thresholds or rules — those live elsewhere in the prompt.
- Do NOT tell the model to "optimize to pass the validator".
- Stay direction-agnostic and economically grounded."""


def propose_role_revision(analysis: Dict) -> Optional[Dict]:
    """
    PROPOSE-ONLY. If a systematic failure pattern dominates the recent batch,
    ask the LLM whether the Role section should be revised. Saves a proposal
    file + sends a Telegram summary. NEVER edits thesis.md.

    Returns the proposal dict if one was written, else None.
    """
    pattern = _dominant_failure_pattern(analysis)
    if not pattern:
        print('  [Role] No dominant failure pattern — no Role proposal.')
        return None

    if _role_proposal_on_cooldown():
        print(f'  [Role] On cooldown (<{ROLE_PROPOSAL_COOLDOWN_HOURS}h since last proposal) — skipping.')
        return None

    current_role = extract_current_role()
    if not current_role:
        print('  [Role] ROLE markers not found in thesis.md — cannot propose.')
        return None

    rationales = analysis.get('recent_rationales', [])[:8]
    rationale_block = '\n'.join(f'    - {r}' for r in rationales) or '    (none recorded)'
    prompt = _ROLE_PROPOSAL_PROMPT.format(
        stage=pattern['stage'], count=pattern['count'], total=pattern['total'],
        pct=int(pattern['fraction'] * 100),
        avg_is=analysis.get('avg_is', 0), avg_wf=analysis.get('avg_wf', 0),
        rationales=rationale_block, current_role=current_role,
    )

    print(f'  [Role] Dominant pattern: {pattern["stage"]} '
          f'({pattern["count"]}/{pattern["total"]}) — asking LLM for a Role proposal...')
    raw = call_llm('You are a quant research lead reviewing a strategy-generation prompt.', prompt)
    if not raw:
        print('  [Role] LLM call failed — no proposal.')
        return None

    raw = raw.strip()
    if raw.upper().startswith('NO_CHANGE'):
        print('  [Role] LLM judged no Role change warranted (NO_CHANGE).')
        return None
    if not raw.upper().startswith('PROPOSE'):
        print('  [Role] LLM output not in expected format — discarding.')
        return None

    proposed_role = raw[len('PROPOSE'):].strip().strip('"').strip()
    if len(proposed_role) < 50:
        print('  [Role] Proposed Role text too short — discarding.')
        return None

    proposal = {
        'timestamp': datetime.now().isoformat(),
        'pattern': pattern,
        'avg_is': analysis.get('avg_is', 0),
        'avg_wf': analysis.get('avg_wf', 0),
        'current_role': current_role,
        'proposed_role': proposed_role,
    }
    _save_role_proposal(proposal)
    _notify_role_proposal(proposal)
    return proposal


def _save_role_proposal(proposal: Dict) -> Path:
    """Persist a proposal as a human-readable + machine-readable file."""
    ROLE_PROPOSALS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = ROLE_PROPOSALS_DIR / f'role_proposal_{ts}.md'
    p = proposal['pattern']
    path.write_text(
        f'# Role-revision proposal — {proposal["timestamp"]}\n\n'
        f'**Trigger:** dominant failure stage `{p["stage"]}` '
        f'({p["count"]}/{p["total"]} = {int(p["fraction"]*100)}% of idea-quality failures)\n'
        f'**avg_IS={proposal["avg_is"]}  avg_WF={proposal["avg_wf"]}**\n\n'
        f'## CURRENT Role\n\n```\n{proposal["current_role"]}\n```\n\n'
        f'## PROPOSED Role\n\n```\n{proposal["proposed_role"]}\n```\n\n'
        f'---\nTo apply: review the diff, then run\n'
        f'`python meta_review.py --apply-role-proposal`\n'
        f'(or tell Claude "apply the role proposal").\n'
    )
    # machine-readable sibling for apply step
    (path.with_suffix('.json')).write_text(json.dumps(proposal, indent=2))
    print(f'  [Role] Proposal saved: {path.name}')
    return path


def _notify_role_proposal(proposal: Dict) -> None:
    """Send a short Telegram summary; swallow failures (notify is best-effort)."""
    try:
        from telegram_bot import notify_html
        p = proposal['pattern']
        msg = (
            '<b>Role-revision proposal</b>\n'
            f'Trigger: <code>{p["stage"]}</code> dominates '
            f'{p["count"]}/{p["total"]} ({int(p["fraction"]*100)}%)\n'
            f'avg_IS={proposal["avg_is"]} avg_WF={proposal["avg_wf"]}\n\n'
            'Proposed new Role:\n'
            f'<i>{proposal["proposed_role"][:500]}</i>\n\n'
            'Not applied. Approve by telling Claude "apply the role proposal".'
        )
        notify_html(msg)
    except Exception as e:
        print(f'  [Role] Telegram notify skipped: {e}')


def apply_role_proposal(proposal_path: Optional[str] = None) -> bool:
    """
    Apply a saved Role proposal to thesis.md by swapping the ROLE block.
    Run ONLY after human approval. Uses the newest proposal if path omitted.
    """
    if proposal_path:
        json_path = Path(proposal_path)
        if json_path.suffix != '.json':
            json_path = json_path.with_suffix('.json')
    else:
        if not ROLE_PROPOSALS_DIR.exists():
            print('No .role-proposals directory.')
            return False
        jsons = sorted(ROLE_PROPOSALS_DIR.glob('role_proposal_*.json'))
        if not jsons:
            print('No saved Role proposals.')
            return False
        json_path = jsons[-1]

    if not json_path.exists():
        print(f'Proposal not found: {json_path}')
        return False

    proposal = json.loads(json_path.read_text())
    proposed_role = proposal['proposed_role']

    content = THESIS_MD.read_text()
    s = content.find(ROLE_START_MARKER)
    e = content.find(ROLE_END_MARKER)
    if s == -1 or e == -1:
        print('ERROR: ROLE markers not found in thesis.md')
        return False
    s += len(ROLE_START_MARKER)
    new_content = content[:s] + '\n' + proposed_role.strip() + '\n' + content[e:]
    THESIS_MD.write_text(new_content)
    print(f'  ✓ Applied Role proposal from {json_path.name} to thesis.md')
    return True


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run_meta_review(trigger_threshold: int = 15) -> str:
    """
    Run LLM-powered meta-review.
    1. Fetch recent results from DB
    2. Analyze patterns
    3. Try LLM directive generation
    4. Fall back to rule-based on failure
    5. Update program.md
    """
    print(f'[Meta-Review] {datetime.now().isoformat()}')

    # Step 1: Fetch data
    results = get_recent_results(limit=30)
    print(f'  Fetched {len(results)} results from DB')

    if len(results) < 5:
        print('  Too few results for meaningful analysis. Skipping.')
        return ''

    # Step 2: Pattern analysis
    analysis = analyze_patterns(results)
    print(f'  Pattern analysis: avg_IS={analysis["avg_is"]:.4f} avg_WF={analysis["avg_wf"]:.4f}')
    print(f'  Regime silence: {analysis["regime_silence"]}/{analysis["total"]} | Low IS: {analysis["low_is"]}/{analysis["total"]}')

    # Step 3: Get current directive (avoid repetition)
    current_directive = extract_current_directive()
    if current_directive:
        print(f'  Current directive: {current_directive[:60]}...')

    # Step 4: Try LLM first
    directive = None
    llm_raw = None

    if OPENROUTER_API_KEY:
        print('  Attempting LLM directive generation...')
        llm_prompt = _build_llm_prompt(analysis, current_directive)
        llm_raw = call_llm('REVIEWER_PROMPT', llm_prompt)

        if llm_raw:
            print(f'  LLM raw output: {llm_raw[:100]}...')
            # Parse: expect 3 bullets, each starting with "- "
            bullets = []
            for line in llm_raw.split('\n'):
                line = line.strip()
                if line.startswith('- '):
                    bullets.append(line)
                elif line.startswith('-'):
                    bullets.append('- ' + line[1:].strip())

            # Valid: need at least 2 bullets
            if len(bullets) >= 2:
                directive = '\n'.join(bullets[:3])
                print(f'  LLM generated {len(bullets)} directives')
            else:
                print(f'  LLM returned {len(bullets)} bullets — too few, using fallback')
        else:
            print('  LLM failed or no API key — using rule-based fallback')
    else:
        print('  No OpenRouter API key — using rule-based fallback')

    # Step 5: Fallback if no directive
    if not directive:
        print('  Generating rule-based directive...')
        directive = generate_rule_based_directive(analysis)

    # Step 6: Update program.md
    if update_research_phase(directive):
        print(f'  ✓ program.md updated')
        print(f'  New directive: {directive[:80]}...')
    else:
        print('  ✗ Failed to update program.md')

    # Step 7: (advisory) Propose a Role-section revision IF a systematic
    # failure pattern dominates the batch. Propose-only — never auto-applied.
    if OPENROUTER_API_KEY:
        try:
            propose_role_revision(analysis)
        except Exception as e:
            print(f'  [Role] proposal step skipped: {e}')

    return directive


# ============================================================================
# CLI
# ============================================================================

if __name__ == '__main__':
    import sys
    if '--apply-role-proposal' in sys.argv:
        # Optional explicit path after the flag
        idx = sys.argv.index('--apply-role-proposal')
        path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        apply_role_proposal(path)
    else:
        run_meta_review()