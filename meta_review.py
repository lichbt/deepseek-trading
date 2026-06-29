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
import math
import requests
from collections import Counter
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

DB_PATH = Path(__file__).parent / 'pipeline.db'
PROGRAM_MD = Path(__file__).parent / 'program.md'
THESIS_MD  = Path(__file__).parent / 'thesis.md'
REVIEWER_MD = Path(__file__).parent / 'reviewer.md'
# Clean-era cutoff: the diversity steerer learns ONLY from generations after the
# 2026-06-29 generation-config overhaul (loosened Role, rebalanced constraints,
# calendar archetype) — excludes the ~30k contaminated pre-fix corpus. A date filter,
# NOT a new DB: the live book + history stay in pipeline.db untouched.
CLEAN_ERA_START = os.getenv('CLEAN_ERA_START', '2026-06-30')
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

Strategy-family survival (which designs get past the IS gate):
{family_breakdown}

Near-miss themes (reached WF/holdout, just missed — steer EXPLORATION toward these families/instruments; do NOT prescribe specific structures to clone):
{near_miss_themes}

Demonstrated real edge, blocked ONLY by drawdown (passed WF + torture across history, failed the DD gate — these instrument families HAVE genuine edge but blow the drawdown limit; prioritise DD-CONTROLLED designs for them — regime gating, tighter stops, smaller size — rather than avoiding them):
{dd_blocked}

Recent failed rationales:
{failed_rationales}

Current directive:
{current_directive}

Generate 3 new bullet points (under 100 chars each) for research focus. Base them on the OVERALL picture above: push toward families/instruments that survive or near-miss, away from those dying at the IS gate. Coarse direction only."""


# ============================================================================
# DATABASE HELPERS
# ============================================================================

# Results scored before the macro publication-lag fix (commit 91daa4f,
# 2026-06-10) were graded against unpublished macro data — the look-ahead
# leak. Mixing eras teaches the generator that leak-favoured shapes "work"
# (it spent a week flooding NZD/H4 intraday-macro ideas for exactly this
# reason), so pattern analysis and role proposals learn from honest-era
# results only.
HONEST_ERA_START = '2026-06-10T02:00:00'


def get_recent_results(limit: int = 30) -> List[Dict]:
    """Fetch recent validation results from DB (honest era only)."""
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
        WHERE v.tested_at >= ?
        ORDER BY v.tested_at DESC
        LIMIT ?
    ''', (HONEST_ERA_START, limit))
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

def _classify_gate(status: str) -> str:
    """Map a failed final_status string to its gate bucket. Single source of
    truth for gate classification (gate_counts + family/near-miss stats)."""
    s = (status or '').lower()
    if 'duplicate' in s:
        return 'duplicate'
    if 'code error' in s or 'syntax' in s:
        return 'code'
    if 'data' in s or 'candles' in s:
        return 'data'
    # The validator has emitted two phrasings across versions — abbreviated
    # ("IS 0.18 < 0.3", "WF 0.0 < 0.5") and full ("In-sample GT-Score 0.18 ...",
    # "Walk-forward GT-Score 0.0 ..."). Match BOTH, or older/mixed windows
    # silently dump real IS/WF failures into 'other' and skew the dominance gate.
    if s.startswith('fail: is') or ' is ' in s or 'in-sample' in s:
        return 'is'
    if 'sparse trades' in s:
        return 'sparse'
    if 'holdout' in s or 'decay' in s:
        return 'holdout'
    # 'single-regime edge: N/M windows' and 'min window GT-Score' are walk-forward
    # robustness failures — previously mis-bucketed as 'other', understating WF.
    if ('wf' in s or 'walk forward' in s or 'walk-forward' in s
            or 'single-regime' in s or 'window' in s):
        return 'wf'
    return 'other'


def _infer_family(code: str) -> str:
    """Coarse strategy archetype (macro/session/news/pair/spread/standard) from
    the code. Lazy import keeps meta_review decoupled; fail-safe to 'standard'."""
    try:
        from portfolio import _infer_archetype
        return _infer_archetype(code or '')
    except Exception:
        return 'standard'


def analyze_patterns(results: List[Dict]) -> Dict:
    """Rule-based pattern analysis. Used both as LLM context and fallback."""
    if not results:
        return {'total': 0, 'message': 'No results yet'}

    passed = [r for r in results if 'pass' in (r.get('final_status') or '').lower()]
    failed = [r for r in results if 'fail' in (r.get('final_status') or '').lower()]

    statuses = [r.get('final_status', '') for r in results]
    # Exclude non-finite scores (-inf/nan): a degenerate GT computation stored as
    # -inf would otherwise drag the whole average to -inf and corrupt the trigger.
    is_scores = [r['is_gt_score'] for r in results
                 if r.get('is_gt_score') is not None and math.isfinite(r['is_gt_score'])]
    wf_scores = [r['walk_forward_gt_score'] for r in results
                 if r.get('walk_forward_gt_score') is not None and math.isfinite(r['walk_forward_gt_score'])]

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
    # Per-gate IS/WF score lists, so a Role proposal can report the DOMINANT
    # COHORT's avg/max (the strategies that failed AT that stage) instead of the
    # window-wide average. The window-wide average blends edgeless IS-failures
    # with the much higher scores of strategies that CLEARED the IS gate, which
    # made the trigger unreadable (2026-06-14: a headline avg_IS=0.34 hid an
    # is-cohort that actually averaged ~0.06 — masking "edgeless ideas dying at
    # the gate" as "borderline overfit" and firing a bad core-prompt proposal).
    gate_scores = {g: {'is': [], 'wf': []} for g in gate_counts}

    inst_stats = {}
    tf_stats = {}
    # Family survival ("which designs get past the IS gate") + near-miss themes
    # ("reached WF/holdout, just missed") — coarse direction signal for the
    # directive LLM. Deliberately family/instrument-level, NOT specific-structure:
    # near-misses say "explore this family more", never "clone this strategy".
    arch_stats = {}
    near_misses = []

    for r in results:
        status = (r.get('final_status') or '').lower()
        timeframe = r.get('timeframe', 'D')
        instrument = r.get('instrument', 'unknown')
        passed_flag = 'pass' in status
        gate = None if passed_flag else _classify_gate(status)

        if instrument not in inst_stats:
            inst_stats[instrument] = {'total': 0, 'passed': 0, 'failed': 0, 'avg_is': []}
        inst_stats[instrument]['total'] += 1
        inst_stats[instrument]['passed' if passed_flag else 'failed'] += 1
        if r.get('is_gt_score') is not None and math.isfinite(r['is_gt_score']):
            inst_stats[instrument]['avg_is'].append(r['is_gt_score'])

        if timeframe not in tf_stats:
            tf_stats[timeframe] = {'total': 0, 'passed': 0, 'failed': 0, 'wf_zeros': 0}
        tf_stats[timeframe]['total'] += 1
        tf_stats[timeframe]['passed' if passed_flag else 'failed'] += 1
        if r.get('walk_forward_gt_score') == 0.0:
            tf_stats[timeframe]['wf_zeros'] += 1

        if not passed_flag:
            gate_counts[gate] += 1
            _is = r.get('is_gt_score')
            _wf = r.get('walk_forward_gt_score')
            if _is is not None and math.isfinite(_is):
                gate_scores[gate]['is'].append(_is)
            if _wf is not None and math.isfinite(_wf):
                gate_scores[gate]['wf'].append(_wf)

        # Family survival: a strategy "reached WF" if it got past the IS gate
        # (IS / code / data / duplicate failures never compute a WF score).
        arch = _infer_family(r.get('code') or '')
        ast = arch_stats.setdefault(arch, {'total': 0, 'passed': 0, 'reached_wf': 0})
        ast['total'] += 1
        if passed_flag:
            ast['passed'] += 1
            ast['reached_wf'] += 1
        elif gate in ('wf', 'sparse', 'holdout', 'other'):
            ast['reached_wf'] += 1

        # Near-miss: cleared WF but failed holdout (had real WF edge), or landed
        # just under the WF bar. These cluster around genuine edges worth more
        # exploration — surfaced as themes, never as clone-this-strategy.
        if not passed_flag:
            wf = r.get('walk_forward_gt_score')
            ho = r.get('holdout_gt_score')
            if gate == 'holdout':
                near_misses.append({'inst': instrument, 'arch': arch, 'tf': timeframe,
                                    'wf': wf, 'ho': ho, 'why': 'reached holdout'})
            elif gate == 'wf' and isinstance(wf, (int, float)) and math.isfinite(wf) and 0.4 <= wf < 0.5:
                near_misses.append({'inst': instrument, 'arch': arch, 'tf': timeframe,
                                    'wf': wf, 'ho': ho, 'why': 'WF just under bar'})

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
        'gate_scores': gate_scores,
        'inst_stats': inst_stats,
        'tf_stats': tf_stats,
        'arch_stats': arch_stats,
        'near_misses': near_misses[:20],
        'recent_rationales': [r.get('rationale', '') for r in failed[:10] if r.get('rationale')],
    }


# ============================================================================
# LLM DIRECTIVE GENERATION
# ============================================================================

# Meta-review LLM (OpenRouter). Returns RAW TEXT (directive bullets, or
# PROPOSE/NO_CHANGE), not JSON.
#
# COST: switched off the paid DeepSeek V4 Pro reasoning model (2026-06-04). It
# was billing hidden-reasoning tokens on every directive call and the trigger
# fired ~4x/batch around the clock (~$5/day) for output that's a simple 3-bullet
# directive plus the occasional role proposal (which a human reviews before
# applying anyway). Free gpt-oss-120b handles both fine; a second free model
# backs it up. META_MAX_TOKENS stays generous so reasoning-style models don't
# truncate `content` with finish_reason="length".
META_MODEL = 'openai/gpt-oss-120b:free'                # free primary (routine directive)
META_MODEL_FALLBACK = 'deepseek/deepseek-v4-flash:free'  # free backstop
META_MAX_TOKENS = 4000

# Directive analysis window. The per-batch directive used to read only the last
# 30 results (≈ ONE batch), so it reacted to a single noisy snapshot rather than
# the overall trend — "barely checks the overall" (2026-06-13). Widened to ~3
# batches so directives reflect a stable multi-batch picture. Still re-evaluated
# every trigger; only the lookback is wider.
DIRECTIVE_WINDOW = 100

# Role-revision proposals are rare (<=1/day, 24h cooldown) and quality-sensitive:
# the free model kept dropping/mangling parts of the role when reproducing it.
# Use the paid v4-pro reasoning model JUST for this call (cents/day), with the
# free model as backstop. ROLE_MAX_TOKENS is large because v4-pro spends hidden
# reasoning tokens before emitting content — too small and the role comes back
# truncated (which is what caused earlier dropped-paragraph proposals).
ROLE_MODEL = 'deepseek/deepseek-v4-pro'   # paid reasoning model, role proposals only
ROLE_MAX_TOKENS = 8000


def call_llm(system_prompt: str, user_prompt: str, model: str = None,
             max_tokens: int = None) -> Optional[str]:
    """
    Call OpenRouter for meta-review text generation. Returns the model's raw
    text output, or None on any failure (callers fall back to rule-based).

    With no explicit model: tries META_MODEL then META_MODEL_FALLBACK (both free).
    With an explicit model: tries that model then the free META_MODEL_FALLBACK as
    a backstop. max_tokens defaults to META_MAX_TOKENS.
    """
    if not OPENROUTER_API_KEY:
        print('  LLM: no OPENROUTER_API_KEY — skipping LLM')
        return None

    # Load reviewer system prompt if using the sentinel
    if system_prompt == 'REVIEWER_PROMPT':
        system_prompt = get_reviewer_system_prompt()

    tok = max_tokens or META_MAX_TOKENS
    models = [model, META_MODEL_FALLBACK] if model else [META_MODEL, META_MODEL_FALLBACK]
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
            'max_tokens': tok,
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


def dd_blocked_families(min_wf: float = 0.5, top: int = 8) -> str:
    """Instruments with DEMONSTRATED real edge (WF>=min_wf, clean torture) that
    failed ONLY the drawdown gate, across ALL history. These are the
    'real edge but too risky' near-wins — rare and slow to accumulate, so a WIDE
    window, not the recent directive window. Steers the generator to design
    DD-CONTROLLED edges for these families rather than ignore them. '' on error.
    (Seeded 2026-06-16 by the reclaim of pre-honest-era price-based candidates.)"""
    try:
        items = _dd_blocked_counter(min_wf).most_common(top)
        if not items:
            return ''
        return '\n'.join(f"  {inst}: {c} (real edge, blew drawdown)" for inst, c in items)
    except Exception:
        return ''


def _dd_blocked_counter(min_wf: float = 0.5):
    """Counter of instruments with real edge (WF>=min_wf, clean torture) that
    failed ONLY the drawdown gate, across ALL history (excludes 'unknown')."""
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT strategy_id FROM validation_results "
        "WHERE walk_forward_gt_score >= ? "
        "AND (torture_flags IS NULL OR torture_flags = '[]') "
        "AND lower(final_status) LIKE '%max drawdown%'", (min_wf,)).fetchall()
    conn.close()
    return Counter(i for i in (_infer_instrument_from_id(r['strategy_id']) for r in rows)
                   if i != 'unknown')


def dd_blocked_instruments(min_wf: float = 0.5) -> list:
    """Ordered DD-blocked-edge instruments (most-frequent first), or [] on error.
    Seeds auto_research's bounded 'exploit' slots — families with demonstrated
    real edge that need drawdown control."""
    try:
        return [inst for inst, _ in _dd_blocked_counter(min_wf).most_common()]
    except Exception:
        return []


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

    # Family survival — which archetypes get past the IS gate (sorted by volume)
    fam_lines = []
    for arch, st in sorted(analysis.get('arch_stats', {}).items(),
                           key=lambda kv: -kv[1].get('total', 0)):
        fam_lines.append(f"  {arch}: {st.get('reached_wf', 0)}/{st.get('total', 0)} "
                         f"reached WF, {st.get('passed', 0)} passed")
    family_breakdown = '\n'.join(fam_lines) or '  (none)'

    # Near-miss themes — coarse (archetype × instrument) clusters, not structures
    theme_counts = Counter(
        (m.get('arch', '?'), m.get('inst', '?')) for m in analysis.get('near_misses', [])
    )
    nm_lines = [f"  {arch} on {inst}: {c} near-miss(es)"
                for (arch, inst), c in theme_counts.most_common(6)]
    near_miss_themes = '\n'.join(nm_lines) or '  (none)'

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
        family_breakdown=family_breakdown,
        near_miss_themes=near_miss_themes,
        dd_blocked=(analysis.get('dd_blocked') or '  (none)'),
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
ROLE_REVIEWER_MD = Path(__file__).parent / 'role_reviewer.md'
# Fire when one failure stage holds this fraction of (idea-quality) failures.
# Lowered 0.80 -> 0.55 (2026-06-13): the honest-era reset left failures diffuse
# but still LOPSIDED — e.g. ~68% die at the IS gate, a clearly actionable
# "ideas lack in-sample edge" signal that the 0.80 bar silently suppressed
# (no role proposal fired for 5 days). A simple majority over the wide window
# below is enough signal, and it's still propose-only (a human approves), still
# rate-limited to once/24h — so a lower bar surfaces real patterns without
# thrashing the core prompt.
ROLE_PATTERN_DOMINANCE = 0.55
# Role proposals change the CORE prompt and fire at most once/24h, so the
# dominance % must reflect a PERSISTENT pattern, not one batch. Evaluate the
# trigger over the last ~5 batches rather than the single-batch window the
# per-batch directive uses.
ROLE_PROPOSAL_WINDOW = 150
# Don't propose more than once per this many hours (rate limit).
ROLE_PROPOSAL_COOLDOWN_HOURS = 24
# Hard-gate: an IS-dominant cohort whose AVERAGE in-sample score is below this
# (half the ~0.30 IS gate) is a hopelessly-edgeless distribution — the typical idea
# scores near zero, so no Role wording manufactures edge: force NO_CHANGE without an
# LLM call. Gating on the AVG (not max) spares a cohort clustering just below the
# gate, where a steer might help and the LLM (now with success context) should decide.
EDGELESS_IS_AVG = 0.15


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
    # Cohort-specific score stats for the dominant stage — what the reviewer
    # actually needs to tell "overfit (high IS, ~0 WF)" apart from "edgeless
    # (IS at/below the gate)". WF is only a genuine measurement for stages that
    # actually reached walk-forward; an IS-stage cohort stores a PLACEHOLDER
    # wf=0.0 it never computed, so report its WF as n/a (None) rather than a
    # misleading 0.0.
    scores = (analysis.get('gate_scores') or {}).get(stage, {})
    is_list = [s for s in scores.get('is', []) if s is not None and math.isfinite(s)]
    reached_wf = stage in ('wf', 'sparse', 'holdout', 'other')
    wf_list = ([s for s in scores.get('wf', []) if s is not None and math.isfinite(s)]
               if reached_wf else [])
    return {
        'stage': stage, 'count': count, 'total': total, 'fraction': round(frac, 3),
        'cohort_avg_is': round(sum(is_list) / len(is_list), 4) if is_list else None,
        'cohort_avg_wf': round(sum(wf_list) / len(wf_list), 4) if wf_list else None,
        'cohort_max_is': round(max(is_list), 4) if is_list else None,
        'cohort_n_is': len(is_list),
    }


def _role_proposal_on_cooldown() -> bool:
    """True if a proposal was written within the cooldown window."""
    if not ROLE_PROPOSALS_DIR.exists():
        return False
    # Count ALL prior proposals, including ones already applied/rejected (which get
    # renamed to *.md.applied / *.md.rejected). Globbing only '*.md' meant that
    # acting on a proposal cleared the cooldown, letting a new one fire immediately
    # — which is why several proposals could appear within the 24h window.
    proposals = list(ROLE_PROPOSALS_DIR.glob('role_proposal_*.md')) \
              + list(ROLE_PROPOSALS_DIR.glob('role_proposal_*.md.applied')) \
              + list(ROLE_PROPOSALS_DIR.glob('role_proposal_*.md.rejected'))
    if not proposals:
        return False
    newest_mtime = max(p.stat().st_mtime for p in proposals)
    age_h = (datetime.now().timestamp() - newest_mtime) / 3600.0
    return age_h < ROLE_PROPOSAL_COOLDOWN_HOURS


# Embedded fallback used only if role_reviewer.md is missing. The live prompt
# lives in role_reviewer.md so it can be tuned without code changes.
_ROLE_PROPOSAL_PROMPT_FALLBACK = """You are a quant research lead reviewing the ROLE section of a \
thesis-generation prompt. Decide whether a dominant batch-failure pattern reveals a systematic \
blind spot in the Role wording.

Dominant failure stage: {stage} ({count}/{total} = {pct}% of idea-quality failures)
Dominant-cohort avg in-sample score: {avg_is}   (max in cohort: {cohort_max_is})
Dominant-cohort avg walk-forward score: {avg_wf}
NOTE: scores are for the strategies that failed AT the dominant stage. "Overfit"
means high IS with ~0 WF; if the cohort's IS is at or below the in-sample gate,
that is edgeless rejection (the validator doing its job) -> NO_CHANGE.
Sample failing rationales:
{rationales}

CURRENT ROLE SECTION:
\"\"\"
{current_role}
\"\"\"

If a revision is warranted, output EXACTLY:
PROPOSE
<the full revised Role section text>
Otherwise output EXACTLY:
NO_CHANGE

Rules: keep the same 2-paragraph shape; no numeric thresholds; never say "optimize to pass the \
validator"; stay direction-agnostic and economically grounded."""


def _load_role_proposal_prompt() -> str:
    """Load the role-proposal prompt template (role_reviewer.md), with fallback."""
    if ROLE_REVIEWER_MD.exists():
        return ROLE_REVIEWER_MD.read_text()
    print(f'  [Role] WARNING: {ROLE_REVIEWER_MD.name} not found — using embedded fallback.')
    return _ROLE_PROPOSAL_PROMPT_FALLBACK


def propose_role_revision(analysis: Dict, force: bool = False) -> Optional[Dict]:
    """
    PROPOSE-ONLY. If a systematic failure pattern dominates the recent batch,
    ask the LLM whether the Role section should be revised. Saves a proposal
    file + sends a Telegram summary. NEVER edits thesis.md.

    force=True bypasses the 24h cooldown (for manual `--propose-role` runs);
    the dominance gate and LLM discretion still apply.

    Returns the proposal dict if one was written, else None.
    """
    pattern = _dominant_failure_pattern(analysis)
    if not pattern:
        print('  [Role] No dominant failure pattern — no Role proposal.')
        return None

    # Hard-gate: an IS-dominant cohort whose AVG in-sample score is hopelessly low
    # is edgeless ideas dying at the first gate — no Role wording manufactures edge,
    # so force NO_CHANGE without spending an LLM call (the reviewer kept proposing on
    # this; 2026-06-17). Genuine overfit (high IS, ~0 WF) still reaches the reviewer.
    # force=True (manual --propose-role) bypasses the gate.
    _cavg = pattern.get('cohort_avg_is')
    if (not force and pattern['stage'] == 'is'
            and _cavg is not None and _cavg < EDGELESS_IS_AVG):
        print(f'  [Role] Edgeless IS cohort (avg {_cavg:.3f} < {EDGELESS_IS_AVG}) — normal '
              f'rejection of no-edge ideas, NO_CHANGE (no LLM call).')
        return None

    if not force and _role_proposal_on_cooldown():
        print(f'  [Role] On cooldown (<{ROLE_PROPOSAL_COOLDOWN_HOURS}h since last proposal) — skipping.')
        return None

    current_role = extract_current_role()
    if not current_role:
        print('  [Role] ROLE markers not found in thesis.md — cannot propose.')
        return None

    rationales = analysis.get('recent_rationales', [])[:8]
    rationale_block = '\n'.join(f'    - {r}' for r in rationales) or '    (none recorded)'
    # Dominant-COHORT stats (the strategies that failed AT the dominant stage),
    # NOT the window-wide average that blends in strategies which cleared this
    # gate. An IS-stage cohort never reaches WF, so its WF reads n/a rather than
    # borrowing the window's passing-strategy WF — that distinction is exactly
    # what lets the reviewer separate "overfit" from "edgeless rejection".
    c_is, c_wf, c_max = pattern.get('cohort_avg_is'), pattern.get('cohort_avg_wf'), pattern.get('cohort_max_is')
    avg_is_str = f'{c_is}' if c_is is not None else 'n/a'
    avg_wf_str = f'{c_wf}' if c_wf is not None else 'n/a (cohort failed before the WF stage)'
    max_is_str = f'{c_max}' if c_max is not None else 'n/a'
    # Success-side context so the reviewer sees WHAT WORKS, not just failures —
    # otherwise it reflexively restricts failing rationales (incl. styles that ALSO
    # win) and can't tell a real blind spot from normal edgeless rejection. (2026-06-17)
    arch_stats = analysis.get('arch_stats', {}) or {}
    fam_lines = [f"    {a}: {st.get('reached_wf', 0)}/{st.get('total', 0)} reached WF, "
                 f"{st.get('passed', 0)} passed"
                 for a, st in sorted(arch_stats.items(), key=lambda kv: -kv[1].get('passed', 0))[:6]]
    family_survival = '\n'.join(fam_lines) or '    (none)'
    nm = Counter((m.get('arch', '?'), m.get('inst', '?')) for m in (analysis.get('near_misses') or []))
    nm_lines = [f"    {a} on {i}: {c} near-miss(es)" for (a, i), c in nm.most_common(6)]
    near_miss_themes = '\n'.join(nm_lines) or '    (none)'
    dd_blocked = analysis.get('dd_blocked') or dd_blocked_families() or '    (none)'
    prompt = _load_role_proposal_prompt().format(
        stage=pattern['stage'], count=pattern['count'], total=pattern['total'],
        pct=int(pattern['fraction'] * 100),
        avg_is=avg_is_str, avg_wf=avg_wf_str, cohort_max_is=max_is_str,
        family_survival=family_survival, near_miss_themes=near_miss_themes,
        dd_blocked=dd_blocked, rationales=rationale_block, current_role=current_role,
    )

    print(f'  [Role] Dominant pattern: {pattern["stage"]} '
          f'({pattern["count"]}/{pattern["total"]}) — asking LLM for a Role proposal...')
    raw = call_llm('You are a quant research lead reviewing a strategy-generation prompt.', prompt,
                   model=ROLE_MODEL, max_tokens=ROLE_MAX_TOKENS)
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
        'avg_is': avg_is_str,   # dominant-cohort avg (see comment above), not window-wide
        'avg_wf': avg_wf_str,
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
        f'**dominant-cohort avg_IS={proposal["avg_is"]} (max {p.get("cohort_max_is")})  '
        f'avg_WF={proposal["avg_wf"]}**\n\n'
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
            f'cohort avg_IS={proposal["avg_is"]} (max {p.get("cohort_max_is")}) '
            f'avg_WF={proposal["avg_wf"]}\n\n'
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

# ---- Data-driven diversity steerer (clean era) --------------------------------
# Buckets recent rationales by MECHANISM (under-used families listed FIRST so a mixed
# rationale counts toward the one we want to encourage). Steers the DISTRIBUTION, not
# pass-rate — safe (won't amplify the beta that 'passes'; see thesis-gen-diversity).
_MECH_BUCKETS = [
    ('calendar',      ['month-end', 'turn-of-month', 'day-of-week', 'seasonal', 'rebalanc', 'expiry', 'calendar', 'weekday']),
    ('cross-market',  ['cross-market', 'related instrument', 'divergence', ' vs ', 'spread between', 'lead-lag', 'inter-market', 'intermarket']),
    ('event',         ['nfp', 'cpi release', 'fomc', 'central bank', 'announcement', 'surprise release', 'scheduled release', 'post-cpi', 'post-nfp']),
    ('volatility',    ['volatil', 'squeeze', 'compress', 'vol regime', 'atr expansion', 'contraction before']),
    ('flow',          ['order flow', 'order-flow', 'liquidity', 'stop-hunt', 'imbalance', 'spread blow']),
    ('carry/macro',   ['carry', 'yield', 'rate differ', 'policy', 'dxy', ' cpi', 'real yield']),
    ('mean-reversion', ['revert', 'mean-revers', 'overreact', 'fade', 'oversold', 'overbought', 'extreme', 'exhaust']),
    ('trend',         ['trend', 'momentum', 'persist', 'breakout', 'continuation']),
]
_MECH_FAMILIES = [name for name, _ in _MECH_BUCKETS]


def _mechanism_of(rationale: str) -> str:
    r = (rationale or '').lower()
    for name, kws in _MECH_BUCKETS:
        if any(k in r for k in kws):
            return name
    return 'other'


def diversity_directive() -> Optional[str]:
    """Data-driven steer: from the CLEAN-ERA mechanism mix, emit one bullet pushing
    the under-used families. Steers the DISTRIBUTION (variety), never pass-rate, so it
    can't amplify beta. Returns None until >=50 clean-era gens accrue (no steering on noise)."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = [r[0] for r in conn.execute(
            "SELECT rationale FROM strategies WHERE created_at >= ? AND rationale IS NOT NULL",
            (CLEAN_ERA_START,)).fetchall()]
        conn.close()
    except Exception:
        return None
    rows = [r for r in rows if r and r.strip()]
    if len(rows) < 50:
        return None
    from collections import Counter
    c = Counter(_mechanism_of(r) for r in rows)
    n = len(rows)
    top, top_n = c.most_common(1)[0]
    under = [f for f in _MECH_FAMILIES if c.get(f, 0) / n < 0.05]
    if not under:
        return None
    return (f"- Mechanism mix ({n} clean-era gens): {top} {top_n * 100 // n}% dominant; "
            f"under-used [{', '.join(under[:3])}] <5% — generate MORE of those, less {top}.")


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

    # Step 1: Fetch data (wide enough to reflect the overall trend, not 1 batch)
    results = get_recent_results(limit=DIRECTIVE_WINDOW)
    print(f'  Fetched {len(results)} results from DB (window={DIRECTIVE_WINDOW})')

    if len(results) < 5:
        print('  Too few results for meaningful analysis. Skipping.')
        return ''

    # Step 2: Pattern analysis
    analysis = analyze_patterns(results)
    # Wide-window "real edge, blocked only by drawdown" steer (rare/slow to
    # accumulate, so all-history, not the recent window).
    analysis['dd_blocked'] = dd_blocked_families()
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

    # Step 5b: prepend the data-driven diversity steer (clean-era mechanism mix),
    # capping the RESEARCH_PHASE block to 3 bullets total. Safe: steers variety, not
    # pass-rate. No-op until enough clean-era data has accrued.
    div = diversity_directive()
    if div:
        keep = [b for b in directive.split('\n') if b.strip().startswith('-')][:2]
        directive = '\n'.join([div] + keep)
        print(f'  + diversity steer: {div[:72]}...')

    # Step 6: Update program.md
    if update_research_phase(directive):
        print(f'  ✓ program.md updated')
        print(f'  New directive: {directive[:80]}...')
    else:
        print('  ✗ Failed to update program.md')

    # Step 7: (advisory) Propose a Role-section revision IF a systematic failure
    # pattern PERSISTS across multiple batches. Propose-only — never auto-applied.
    # Uses a wider window than the per-batch directive above so a single skewed
    # batch can't trigger a core-prompt change (the 24h cooldown means this fires
    # at most once/day, so it should reflect a stable pattern, not one snapshot).
    if OPENROUTER_API_KEY:
        try:
            role_results = get_recent_results(limit=ROLE_PROPOSAL_WINDOW)
            role_analysis = analyze_patterns(role_results) if len(role_results) >= 5 else analysis
            propose_role_revision(role_analysis)
        except Exception as e:
            print(f'  [Role] proposal step skipped: {e}')

    return directive


def run_role_proposal(force: bool = True) -> Optional[Dict]:
    """
    Manual entry point: analyze recent results and run ONLY the Role-proposal
    step on demand (bypassing the 15-consecutive-failures gate). The dominance
    gate and LLM discretion still apply; force defaults to True so the 24h
    cooldown doesn't block a deliberate test.
    """
    print(f'[Role-Proposal] {datetime.now().isoformat()}')
    results = get_recent_results(limit=ROLE_PROPOSAL_WINDOW)
    print(f'  Fetched {len(results)} results from DB (window={ROLE_PROPOSAL_WINDOW})')
    if len(results) < 5:
        print('  Too few results for meaningful analysis. Skipping.')
        return None
    analysis = analyze_patterns(results)
    pattern = _dominant_failure_pattern(analysis)
    if not pattern:
        gc = analysis.get('gate_counts', {})
        print(f'  No dominant idea-quality pattern (>= {int(ROLE_PATTERN_DOMINANCE*100)}% '
              f'of >=5 failures). gate_counts={gc}')
        return None
    return propose_role_revision(analysis, force=force)


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
    elif '--propose-role' in sys.argv:
        # Manual on-demand Role proposal (bypasses cooldown; still needs a
        # dominant pattern + LLM PROPOSE verdict).
        run_role_proposal(force=True)
    else:
        run_meta_review()