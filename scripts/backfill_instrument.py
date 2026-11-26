"""Backfill strategies.instrument from the strategy id.

WHY. The column was added later as nullable ("let callers infer where possible",
pipeline_utils.init_db) and never backfilled, so most historical rows carry NULL.
Any analysis that reads it then has to invent a default, and a default is exactly
the wrong move: a sweep here defaulted NULL to EUR_USD and silently re-ran ~127
strategies against a market they were never designed for, producing a tally that
looked plausible and meant nothing. Backfilling removes the temptation.

The id always carries the instrument (mean_reversion_eur_jpy_v18,
btcusd_auto_20260716_204438_i19), so inference is reliable — but it is inference,
so anything unresolvable is LEFT NULL rather than guessed, and --dry-run is the
default.

Usage:
    python3 scripts/backfill_instrument.py              # report only
    python3 scripts/backfill_instrument.py --write      # apply
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline_utils import DB_PATH  # noqa: E402

# A lone currency in an id means its USD pair — that is the naming convention
# these strategies were generated under (mean_reversion_eur_v4 is EUR_USD).
_BARE = (
    ('eur', 'EUR_USD'), ('gbp', 'GBP_USD'), ('jpy', 'USD_JPY'),
    ('aud', 'AUD_USD'), ('nzd', 'NZD_USD'), ('chf', 'USD_CHF'),
    ('cad', 'USD_CAD'), ('xau', 'XAU_USD'), ('xag', 'XAG_USD'),
    ('btc', 'BTC_USD'), ('eth', 'ETH_USD'), ('ltc', 'LTC_USD'),
)

# Commodity ids name their market in words rather than in symbols
# (gold_reversal_cci_v86, soybean_harvest_supply_shock_recovery). Deliberately
# no bare "oil": BCO_USD and WTICO_USD would both claim it, and a coin-flip
# between two real instruments is worse than leaving the row NULL.
_WORDS = (
    ('gold', 'XAU_USD'), ('silver', 'XAG_USD'), ('copper', 'XCU_USD'),
    ('platinum', 'XPT_USD'), ('palladium', 'XPD_USD'),
    ('soybean', 'SOYBN_USD'), ('wheat', 'WHEAT_USD'), ('corn', 'CORN_USD'),
    ('natgas', 'NATGAS_USD'), ('brent', 'BCO_USD'),
    # wtico before wti: the longer spelling must win, though both resolve here.
    ('wtico', 'WTICO_USD'), ('wti', 'WTICO_USD'),
)


def infer_instrument(strategy_id: str, known):
    """Instrument for a strategy id, or None if it cannot be determined.

    Full pairs are matched LONGEST FIRST so eur_jpy wins over a bare eur — the
    reverse order silently files every cross as its base currency's USD pair.
    Both the underscored and squashed spellings are accepted (eur_jpy, eurjpy).
    """
    s = strategy_id.lower()
    for p in sorted(known, key=len, reverse=True):
        pl = p.lower()
        if pl in s or pl.replace('_', '') in s:
            return p
    for base, full in _BARE:
        if re.search(rf'(^|_){base}(_|$)', s) and full in known:
            return full
    for word, full in _WORDS:
        if word in s and full in known:
            return full
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true',
                    help='apply the backfill (default is a dry run)')
    a = ap.parse_args()

    con = sqlite3.connect(str(DB_PATH))
    known = [r[0] for r in con.execute(
        'SELECT DISTINCT instrument FROM strategies WHERE instrument IS NOT NULL')]
    if not known:
        print('no known instruments to match against — aborting')
        return 1

    # Self-check FIRST: the inference must reproduce every instrument that is
    # already recorded. If it cannot, it has no business writing the ones that
    # are missing.
    rows = con.execute(
        'SELECT id, instrument FROM strategies WHERE instrument IS NOT NULL').fetchall()
    bad = [(sid, known_i, infer_instrument(sid, known))
           for sid, known_i in rows if infer_instrument(sid, known) != known_i]
    print(f'self-check against {len(rows)} rows that already have an instrument: '
          f'{len(rows) - len(bad)} match, {len(bad)} mismatch')
    for sid, k, g in bad[:15]:
        print(f'  MISMATCH {sid}  recorded={k}  inferred={g}')
    if bad:
        print('\ninference disagrees with recorded data — refusing to write')
        return 1

    nulls = [r[0] for r in con.execute(
        'SELECT id FROM strategies WHERE instrument IS NULL')]
    resolved = {sid: infer_instrument(sid, known) for sid in nulls}
    unresolved = [sid for sid, v in resolved.items() if v is None]

    print(f'\nNULL instrument rows: {len(nulls)}')
    print(f'  resolvable:   {len(nulls) - len(unresolved)}')
    print(f'  unresolvable: {len(unresolved)}  (left NULL, never guessed)')
    for sid in unresolved[:15]:
        print(f'    {sid}')

    if not a.write:
        print('\ndry run — pass --write to apply')
        return 0

    con.executemany('UPDATE strategies SET instrument = ? WHERE id = ?',
                    [(v, k) for k, v in resolved.items() if v])
    con.commit()
    print(f'\nwrote {len(nulls) - len(unresolved)} rows')
    remaining = con.execute(
        'SELECT COUNT(*) FROM strategies WHERE instrument IS NULL').fetchone()[0]
    print(f'still NULL: {remaining}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
