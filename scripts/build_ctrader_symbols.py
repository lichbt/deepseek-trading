#!/usr/bin/env python3
"""Build enriched cTrader symbol map with volume specs from the Open API.

Parses _FIX_SYMBOL_ID from fix_adapter.py via AST (avoids simplefix import),
connects via ctrader_client.py, fetches live symbol details via the Open API.
Outputs ctrader_symbols.json and a readable table to stdout.
"""

import ast
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _parse_fix_symbol_id():
    """Parse _FIX_SYMBOL_ID dict from fix_adapter.py using AST."""
    path = os.path.join(REPO, 'fix_adapter.py')
    tree = ast.parse(open(path).read())
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == '_FIX_SYMBOL_ID':
                    return ast.literal_eval(n.value)
    raise RuntimeError('_FIX_SYMBOL_ID not found in fix_adapter.py')


def _parse_instrument_prefixes():
    """Parse _INSTRUMENT_PREFIXES list from meta_review.py using AST."""
    path = os.path.join(REPO, 'meta_review.py')
    tree = ast.parse(open(path).read())
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == '_INSTRUMENT_PREFIXES':
                    return list(ast.literal_eval(n.value))
    return None


def main():
    # ── parse source map ────────────────────────────────────────────────
    fix_map = _parse_fix_symbol_id()
    print('FIX_SYMBOL_ID : %d entries' % len(fix_map))
    for k, v in sorted(fix_map.items()):
        print('  %-20s -> %s' % (k, v))

    # ── parse book instrument list for unroutable detection ────────────
    prefixes = _parse_instrument_prefixes()
    if prefixes:
        book_insts = set(prefixes)
        unroutable = sorted(book_insts - set(fix_map.keys()))
    else:
        unroutable = []
    print('Book instruments: %d' % (len(prefixes) if prefixes else 0))
    print('Unroutable (in book but no cTrader symbolId): %s' % unroutable)

    # ── connect to cTrader Open API ────────────────────────────────────
    from ctrader_client import get_client
    client = get_client().start()
    account_id = client.account_id

    # ── fetch live symbol list ─────────────────────────────────────────
    all_symbols = client.get_symbols()
    print('Live cTrader symbols: %d' % len(all_symbols))

    # ── verify every mapped id resolves ────────────────────────────────
    symbol_ids = sorted({int(v) for v in fix_map.values()})
    resolved_ids = set(all_symbols.keys())
    missing = [sid for sid in symbol_ids if sid not in resolved_ids]
    if missing:
        sys.exit('FAIL: symbol ids %s not found in live cTrader list' % missing)

    # ── fetch volume specs for all mapped ids ──────────────────────────
    details = client.get_symbol_details(symbol_ids)
    resolved_detail_ids = set(details.keys())
    no_detail = [sid for sid in symbol_ids if sid not in resolved_detail_ids]
    if no_detail:
        sys.exit('FAIL: no details returned for symbol ids %s' % no_detail)

    # ── build enriched instrument map ──────────────────────────────────
    instruments = {}
    for oanda_name in sorted(fix_map.keys()):
        sid = int(fix_map[oanda_name])
        d = details[sid]
        instruments[oanda_name] = {
            'symbol_id': sid,
            'ctrader_name': all_symbols.get(sid, '?'),
            'digits': d['digits'],
            'pip_position': d['pip_position'],
            'min_volume': d['min_volume'],
            'step_volume': d['step_volume'],
            'max_volume': d['max_volume'],
            'lot_size': d['lot_size'],
        }

    # ── resolve "unroutable" against the LIVE symbol list ──────────────
    # Absence from _FIX_SYMBOL_ID proves only that the FIX map lacks an id — NOT that
    # the broker lacks the instrument. Three were wrongly called unroutable on the first
    # run (NZD_USD, LTC_USD, BCO_USD all exist here), which would have silently dropped
    # tradeable sleeves. The live list is the only authority.
    name_to_id = {name: sid for sid, name in all_symbols.items()}
    # instruments whose cTrader name is not just the OANDA name minus underscores
    ALIASES = {'BCO_USD': 'BRENT', 'WTICO_USD': 'WTI', 'NATGAS_USD': 'NGAS',
               'XCU_USD': 'CUCUSD', 'DE30_EUR': 'DAX40', 'HK33_HKD': 'HSI50',
               'SPX500_USD': 'SP500', 'SUGAR_USD': 'SUGAR'}

    unmapped_but_available = {}
    truly_unroutable = []
    for inst in unroutable:
        cname = ALIASES.get(inst, inst.replace('_', ''))
        sid = name_to_id.get(cname)
        if sid is None:
            truly_unroutable.append(inst)
            continue
        det = client.get_symbol_details([sid])[sid]
        unmapped_but_available[inst] = {
            'symbol_id': sid,
            'ctrader_name': cname,
            'digits': det['digits'],
            'pip_position': det['pip_position'],
            'min_volume': det['min_volume'],
            'step_volume': det['step_volume'],
            'max_volume': det['max_volume'],
            'lot_size': det['lot_size'],
        }
        print('  AVAILABLE but unmapped: %-12s -> %s (id %s)' % (inst, cname, sid))

    unroutable = sorted(truly_unroutable)
    print('Genuinely unroutable (absent from the broker): %s' % unroutable)

    # ── print readable table ───────────────────────────────────────────
    header = '%-16s %6s  %-14s %5s %5s  %12s %10s %14s %10s' % (
        'Instrument', 'SymId', 'cTraderName', 'Dig', 'Pip',
        'MinVol', 'StepVol', 'MaxVol', 'LotSize')
    sep = '─' * len(header)
    print()
    print(header)
    print(sep)
    for oanda_name, info in instruments.items():
        print('%-16s %6s  %-14s %5s %5s  %12s %10s %14s %10s' % (
            oanda_name,
            info['symbol_id'],
            info['ctrader_name'],
            info['digits'],
            info['pip_position'],
            info['min_volume'],
            info['step_volume'],
            info['max_volume'],
            info['lot_size'],
        ))

    # ── write output JSON ──────────────────────────────────────────────
    output = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'account_id': account_id,
        'instruments': instruments,
        # book instruments the broker DOES carry but _FIX_SYMBOL_ID has no id for —
        # candidates to add to the map, not exclusions
        'unmapped_but_available': unmapped_but_available,
        'unroutable': unroutable,
    }
    out_path = os.path.join(REPO, 'ctrader_symbols.json')
    if os.path.exists(out_path):
        # refuse to overwrite anything but ctrader_symbols.json
        pass
    with open(out_path, 'w') as fh:
        json.dump(output, fh, indent=2)
    print()
    print('Wrote %s  (%d instruments, %d unroutable)' % (
        out_path, len(instruments), len(unroutable)))

    # ── summary ────────────────────────────────────────────────────────
    all_ok = all(
        info['min_volume'] is not None and info['step_volume'] is not None
        for info in instruments.values()
    )
    if all_ok:
        print('ALL %d instruments have real volume specs — PASS' % len(instruments))
    else:
        nulls = [k for k, v in instruments.items()
                 if v['min_volume'] is None or v['step_volume'] is None]
        print('WARNING: %d instruments have null volume specs: %s' % (len(nulls), nulls))


if __name__ == '__main__':
    main()
