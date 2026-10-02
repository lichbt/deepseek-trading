"""Cheap candidate retrieval so Jev only judges a 5..20 row shortlist.

Jev is the judge, not the search engine: it cannot find the near-duplicate, only
score a pair it is shown. This module narrows the 100k+ row `strategies` table to
a shortlist using same-instrument recent rows, ranked by token overlap against
the new thesis text.

Lazy on purpose. Keyword overlap, stdlib only, no embedding model, no BM25 dep.
If shadow mode shows the recall gap (real near-dups missing from the shortlist),
upgrade NOTES below, not the interface.
"""

import os
import re
import sqlite3

import pipeline_utils as pu

# ponytail: keyword overlap retrieval; add rank_bm25 or an embedding index only
# if shadow logs show near-duplicates ranked OUTSIDE the top K.
JEV_RETRIEVAL_K = int(os.getenv('JEV_RETRIEVAL_K', '20'))
JEV_RETRIEVAL_POOL = int(os.getenv('JEV_RETRIEVAL_POOL', '400'))

_TOKEN_RE = re.compile(r'[a-z][a-z0-9]{2,}')
_STOP = frozenset(
    'the and for with that from into this its not are was were has have had '
    'will can use using when then than over under above below price prices '
    'strategy entry exit filter condition condition rationale'.split())

def thesis_text(thesis):
    """Flatten the text fields a near-duplicate judgment should see."""
    if isinstance(thesis, str):
        return thesis
    parts = [str(thesis.get(k, '') or '') for k in
             ('rationale', 'entry_condition', 'filter_condition', 'exit_condition')]
    return ' | '.join(p for p in parts if p)

def _tokens(text):
    return {t for t in _TOKEN_RE.findall((text or '').lower()) if t not in _STOP}

def _db_path():
    """Strategies DB. Env override lets a worktree read the main checkout's DB
    (pipeline.db is gitignored, so a fresh worktree has none)."""
    return os.getenv('JEV_PIPELINE_DB') or str(pu.DB_PATH)

# The LIVE BOOK. A new thesis that paraphrases a passed/incubating sleeve is the
# dangerous near-duplicate (it adds book correlation, not information), and there
# are only ~80 of them, so they belong in every shortlist ahead of the recent
# 98k failures. `check_signal_is_new` guards this at the signal level after
# codegen; this is the thesis-text pass on the same cohort.
BOOK_STATUSES = ('passed', 'passed_but_fragile', 'paper_trading', 'incubating')


def _book(instrument, limit=40):
    """Passed/incubating strategies, same instrument first."""
    marks = ','.join('?' * len(BOOK_STATUSES))
    q = (f'SELECT id, rationale FROM strategies WHERE status IN ({marks}) '
         'AND rationale IS NOT NULL AND rationale != ""')
    args = list(BOOK_STATUSES)
    if instrument:
        q += ' AND instrument = ?'
        args.append(instrument)
    q += ' ORDER BY created_at DESC LIMIT ?'
    args.append(limit)
    try:
        with sqlite3.connect(_db_path()) as conn:
            return conn.execute(q, args).fetchall()
    except Exception:
        return []


def _recent(instrument, limit):
    """Most recent `limit` prior strategies, same instrument if given."""
    q = ('SELECT id, rationale FROM strategies '
         'WHERE rationale IS NOT NULL AND rationale != ""')
    args = []
    if instrument:
        q += ' AND instrument = ?'
        args.append(instrument)
    q += ' ORDER BY created_at DESC LIMIT ?'
    args.append(limit)
    try:
        with sqlite3.connect(_db_path()) as conn:
            return conn.execute(q, args).fetchall()
    except Exception:
        return []

def shortlist(thesis, k=None, pool=None):
    """Return {strategy_id: rationale_text} for the k closest prior theses.

    Empty dict = skip Jev (no pool). Same-instrument first; if that pool is too
    thin, falls back to recent rows across instruments (a same-idea thesis on a
    different pair is still a near-duplicate worth judging).
    """
    k = k or JEV_RETRIEVAL_K
    pool = pool or JEV_RETRIEVAL_POOL
    if isinstance(thesis, dict):
        new_t = _tokens(thesis_text(thesis))
        inst = str(thesis.get('instrument', '') or '')
    else:
        new_t = _tokens(thesis)
        inst = ''
    rows = _recent(inst, pool) if inst else []
    if len(rows) < 2:
        rows = _recent('', pool)

    def _scored(pairs):
        return sorted(((len(new_t & _tokens(r)), sid, r) for sid, r in pairs),
                      key=lambda x: -x[0])

    out = {}
    for score, sid, rationale in _scored(_book(inst) or _book(''))[:k]:
        if score > 0:                     # book rows must be topically relevant
            out[sid] = rationale
    for score, sid, rationale in _scored(rows):
        if len(out) >= k:
            break
        if score > 0 and sid not in out:
            out[sid] = rationale
    return out
