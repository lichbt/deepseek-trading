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

    return [dict(r) for r in rows]


def get_current_program_md() -> str:
    """Read the current program.md content."""
    if not PROGRAM_MD.exists():
        return ''
    return PROGRAM_MD.read_text()


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

def call_llm(system_prompt: str, user_prompt: str, model: str = 'deepseek/deepseek-v4-pro') -> Optional[str]:
    """Call OpenRouter for directive generation."""
    if not OPENROUTER_API_KEY:
        print('  LLM: No API key, skipping')
        return None

    # Load reviewer system prompt if using default
    if system_prompt == 'REVIEWER_PROMPT':
        system_prompt = get_reviewer_system_prompt()

    try:
        resp = requests.post(
            f'{OPENROUTER_BASE}/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.3,
                'max_tokens': 600,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data['choices'][0]['message']['content'] or ''
        return content.strip()
    except Exception as e:
        print(f'  LLM: Failed — {e}')
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
# UPDATE PROGRAM.MD
# ============================================================================

def update_research_phase(directive: str) -> bool:
    """Replace the RESEARCH_PHASE section in program.md."""
    if not PROGRAM_MD.exists():
        print('ERROR: program.md not found')
        return False

    content = PROGRAM_MD.read_text()

    start_marker = '<!-- RESEARCH_PHASE_START -->'
    end_marker = '<!-- RESEARCH_PHASE_END -->'

    if start_marker not in content or end_marker not in content:
        print('ERROR: RESEARCH_PHASE markers not found in program.md')
        return False

    start_idx = content.find(start_marker) + len(start_marker)
    end_idx = content.find(end_marker)

    new_content = (
        content[:start_idx]
        + '\n' + directive.strip() + '\n'
        + content[end_idx:]
    )

    PROGRAM_MD.write_text(new_content)
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

    return directive


# ============================================================================
# CLI
# ============================================================================

if __name__ == '__main__':
    run_meta_review()