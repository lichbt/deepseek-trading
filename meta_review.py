#!/usr/bin/env python3
"""
Meta-Review: Analyze failure patterns and generate research directives.
Runs after N consecutive failures to auto-tune the research focus.

Only modifies the section between <!-- RESEARCH_PHASE_START -->
and <!-- RESEARCH_PHASE_END --> in program.md.
All other rules remain immutable.
"""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / 'pipeline.db'
PROGRAM_MD = Path(__file__).parent / 'program.md'


def get_failure_analysis() -> dict:
    """Analyze recent failures to find patterns."""
    if not DB_PATH.exists():
        return {'error': 'pipeline.db not found'}

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    cur.execute('''
        SELECT v.strategy_id, v.final_status, v.is_gt_score, v.walk_forward_gt_score
        FROM validation_results v
        WHERE v.final_status LIKE 'fail%'
        ORDER BY v.tested_at DESC
        LIMIT 30
    ''')
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {'total': 0, 'message': 'No failures to analyze yet'}

    statuses = [r[1] for r in rows if r[1]]
    is_scores = [r[2] for r in rows if r[2] is not None]
    wf_scores = [r[3] for r in rows if r[3] is not None]

    regime_silence = sum(1 for s in statuses if 'WF 0' in s or 'WF 0.0000' in s)
    low_is = sum(1 for s in is_scores if s < 0.1)
    decay = sum(1 for s in statuses if 'decay' in s.lower())
    no_wf_trades = sum(1 for wf in wf_scores if wf == 0.0)

    return {
        'total': len(rows),
        'statuses': statuses,
        'avg_is': round(sum(is_scores) / len(is_scores), 4) if is_scores else 0,
        'avg_wf': round(sum(wf_scores) / len(wf_scores), 4) if wf_scores else 0,
        'regime_silence': regime_silence,
        'low_is': low_is,
        'decay': decay,
        'no_wf_trades': no_wf_trades,
    }


def generate_directive(analysis: dict) -> str:
    """Generate 3-bullet research directive based on failure analysis."""
    if 'error' in analysis:
        return f"- {analysis['error']}\n- All archetypes allowed.\n- All timeframes allowed."
    if 'message' in analysis:
        return f"- {analysis['message']}\n- All archetypes allowed.\n- All timeframes allowed."

    total = analysis['total']
    lines = []

    # Dominant failure mode analysis
    silence_pct = analysis['regime_silence'] / total if total else 0
    low_is_pct = analysis['low_is'] / total if total else 0
    decay_pct = analysis['decay'] / total if total else 0

    # Directive based on dominant pattern
    if silence_pct >= 0.6:
        lines.append(f"- Regime silence dominant ({analysis['regime_silence']}/{total} failed with WF=0). Switch to H4 timeframe for shorter holding periods and more trading opportunities.")
    elif low_is_pct >= 0.6:
        lines.append(f"- Low in-sample scores ({analysis['low_is']}/{total}). Use only 2-3 parameter strategies; simplify indicator combinations.")
    elif decay_pct >= 0.4:
        lines.append(f"- Holdout decay ({analysis['decay']}/{total}). Prefer mean-reversion strategies over trend-following on this dataset.")
    else:
        # Mixed — suggest diversity
        lines.append("- Mixed failure modes; explore non-D timeframes (H4/H1) and avoid pure momentum breakout archetypes.")

    # WF score guidance
    avg_wf = analysis.get('avg_wf', 0)
    if avg_wf < 0.05 and silence_pct < 0.5:
        lines.append(f"- Avg WF score {avg_wf:.4f} is very low; try strategies that trade every 10-20 bars, not just during breakouts.")

    # Final fallback
    if not lines:
        lines.append("- All archetypes allowed; try mean-reversion on EUR/USD or carry-trade on GBP/JPY.")

    return '\n'.join(lines)


def update_research_phase(directive: str) -> bool:
    """Replace the RESEARCH_PHASE section in program.md (append-only block only)."""
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


def run_meta_review(trigger_threshold: int = 15) -> str:
    """Main entry point. Returns the directive that was written."""
    print(f'Meta-review at {datetime.now().isoformat()}')

    analysis = get_failure_analysis()
    print(f'  Analyzed {analysis.get("total", 0)} failures')
    if 'error' in analysis:
        print(f'  ERROR: {analysis["error"]}')
    else:
        print(f'  Avg IS: {analysis.get("avg_is", 0):.4f} | Avg WF: {analysis.get("avg_wf", 0):.4f}')
        print(f'  Regime silence: {analysis.get("regime_silence", 0)} | Low IS: {analysis.get("low_is", 0)} | Decay: {analysis.get("decay", 0)}')

    directive = generate_directive(analysis)
    print(f'  Directive: {directive[:80]}...')

    if update_research_phase(directive):
        print('  program.md updated.')
    else:
        print('  FAILED to update program.md.')

    return directive


if __name__ == '__main__':
    run_meta_review()