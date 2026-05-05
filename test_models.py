#!/usr/bin/env python3
"""A/B model comparison test."""
import os, sys, json, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pipeline_utils import compute_gt_score, compute_strategy_returns, compute_strategy_fingerprint, check_idea_is_new
from data_fetcher import get_candles_date_range
os.environ['OANDA_ACCOUNT_ID'] = '101-011-13677064-003'
os.environ['OANDA_API_TOKEN'] = '43f5e160ff289434d6248e5414cc226f-66bdf18f9199213b719671a19ac96998'

KEY = os.environ.get('OPENROUTER_API_KEY', '')
HEADERS = {'Authorization': f'Bearer {KEY}', 'HTTP-Referer': 'localhost', 'X-Title': 'ModelTest'}

with open('program.md') as f:
    program = f.read()

SYSTEM = """You are a quantitative trading strategy researcher. Generate ONLY valid JSON, no markdown, no code fences."""

USER_TEMPLATE = f"""Based on the research program below, generate a new strategy for {{instrument}} on {{timeframe}} timeframe.

PROGRAM:
{{program}}

Return ONLY valid JSON with exactly these fields:
- strategy_id (unique snake_case name)
- code (complete generate_signals function following the template)
- param_grid (max 4 params)
- rationale (one sentence economic reason)
- timeframe ({{timeframe}})

The code MUST follow the working template structure exactly."""

MODELS = [
    'deepseek/deepseek-chat',
    'meta-llama/llama-3.1-8b-instruct',
]

INSTRUMENTS = ['EUR_USD', 'GBP_USD', 'USD_JPY']
TIMEFRAMES = ['D']
ITERS_PER_MODEL = 5

def extract_json(text):
    text = text.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        if len(lines) > 2:
            text = '\n'.join(lines[1:-1]).strip()
    start = text.find('{')
    end = text.rfind('}') + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except:
            pass
    try:
        return json.loads(text)
    except:
        return None

def call_model(model, instrument, timeframe):
    user = USER_TEMPLATE.format(instrument=instrument, timeframe=timeframe, program=program[:2500])
    r = requests.post('https://openrouter.ai/api/v1/chat/completions',
        headers=HEADERS,
        json={'model': model, 'messages': [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': user}
        ], 'max_tokens': 600, 'temperature': 0.7},
        timeout=60)
    if r.status_code != 200:
        return None, f'HTTP {r.status_code}'
    data = r.json()
    content = data['choices'][0]['message']['content'] or ''
    cand = extract_json(content)
    if not cand:
        return None, f'Parse error: {content[:50]}'
    return cand, None

def eval_candidate(cand):
    code = cand.get('code', '')
    params = cand.get('param_grid', {})
    tf = cand.get('timeframe', 'D')
    inst = cand.get('instrument', 'EUR_USD')

    # Quick eval on 2019 data only (fast)
    df = get_candles_date_range(inst, '2019-01-01', '2019-06-30', tf)
    if len(df) < 50:
        return {'ok': False, 'reason': 'no data'}

    ns = {}
    try:
        exec(code, ns)
        fn = ns['generate_signals']
    except:
        return {'ok': False, 'reason': 'exec error'}

    # Use first param combo
    first_params = {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}

    try:
        signals = fn(df, first_params)
        returns = compute_strategy_returns(df, signals)
        score = compute_gt_score(returns)
        non_zero = int((signals != 0).sum())
        return {'ok': True, 'score': score, 'non_zero': non_zero}
    except Exception as e:
        return {'ok': False, 'reason': f'eval error: {e}'}

def main():
    results = {}
    for model in MODELS:
        results[model] = {'parse_errors': 0, 'exec_errors': 0, 'scores': [], 'non_zero': []}

    for i in range(ITERS_PER_MODEL):
        for model in MODELS:
            inst = INSTRUMENTS[i % len(INSTRUMENTS)]
            tf = TIMEFRAMES[0]

            cand, err = call_model(model, inst, tf)
            if err:
                results[model]['parse_errors'] += 1
                print(f'{model}: parse error: {err}')
                continue

            ev = eval_candidate(cand)
            if not ev['ok']:
                if 'exec' in ev.get('reason', ''):
                    results[model]['exec_errors'] += 1
                print(f'{model}: {ev["reason"]}')
                continue

            results[model]['scores'].append(ev['score'])
            results[model]['non_zero'].append(ev['non_zero'])
            print(f'{model}: IS={ev["score"]:.4f} signals={ev["non_zero"]}')

            time.sleep(1)

    print('\n=== SUMMARY ===')
    for model in MODELS:
        r = results[model]
        scores = r['scores']
        nz = r['non_zero']
        print(f'\n{model}:')
        print(f'  Parse errors: {r["parse_errors"]}')
        print(f'  Exec errors: {r["exec_errors"]}')
        if scores:
            print(f'  Avg IS: {sum(scores)/len(scores):.4f}')
            print(f'  Max IS: {max(scores):.4f}')
            print(f'  Avg signals: {sum(nz)/len(nz):.1f}')
        else:
            print('  No valid scores')

if __name__ == '__main__':
    main()