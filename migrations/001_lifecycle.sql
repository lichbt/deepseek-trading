-- 001_lifecycle.sql — append-only lifecycle store for passed strategies.
--
-- Three tables INSIDE pipeline.db, deliberately not a separate database: a second
-- file means no cross-file transaction and no foreign keys. (The two 0-byte
-- untracked strategies.db / strategy_results.db in the worktree look like an
-- earlier abandoned attempt at exactly that split.)
--
-- Adding tables to a 290 MB SQLite file is metadata-only — no table rewrite —
-- so this is cheap and does not touch a single existing row.
--
-- The append-only TRIGGERS are deliberately NOT here; they land in 002_seal.sql
-- AFTER the strategy_events backfill. Sealing first would mean a botched
-- 148,975-row backfill could only be repaired by dropping the very trigger that
-- exists to prevent history being rewritten.

BEGIN;

-- ---------------------------------------------------------------------------
-- evaluations — one row per evaluate_strategy run.
--
-- This is the table that makes DECAY a queryable time series instead of a number
-- someone recomputes, which is the whole reason the sleeve-ops skill has to
-- mandate reconstruct-don't-assert.
--
-- window_start/window_end are stored WITH the scores on purpose. evaluate_strategy
-- computes FULL_END as (now - 1 day), so it moves every run; two runs are simply
-- not comparable unless the window travels alongside the numbers. (A hard-coded
-- FULL_END once silently scored later runs on stale data — see the 2026-07-25
-- decision.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluations (
    id                 INTEGER PRIMARY KEY,
    strategy_id        TEXT    NOT NULL,
    run_at             TEXT    NOT NULL,   -- UTC ISO8601, supplied by the writer
    window_start       TEXT    NOT NULL,
    window_end         TEXT    NOT NULL,

    -- decay verdict, straight from recent_entry_decay()
    recent_gt          REAL,
    gt_floor           REAL,               -- the threshold it was scored against
    decay_status       TEXT,               -- OK | DECAYED | INSUFFICIENT
    near_miss          INTEGER,            -- 0/1
    entries_in_window  INTEGER,
    entries_lifetime   INTEGER,
    capped_by          TEXT,

    -- headline metrics, straight from metrics()
    r12                REAL,               -- trailing 12mo return
    sharpe             REAL,
    maxdd              REAL,
    inmkt              REAL,               -- fraction of bars in market
    tot_return         REAL,

    verdict            TEXT,               -- the tool's own summary line
    source             TEXT NOT NULL DEFAULT 'live',   -- 'live' | 'backfill'

    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);

CREATE INDEX IF NOT EXISTS idx_evaluations_sid_run
    ON evaluations (strategy_id, run_at);

-- ---------------------------------------------------------------------------
-- strategy_events — structured reason_code BESIDE the existing prose.
--
-- status_history holds 148,975 rows across 8,854 distinct (new_status, reason)
-- pairs. The prose is genuinely good and is kept verbatim in reason_prose —
-- nothing is lost. What it cannot do is GROUP BY, so "why do strategies fail?"
-- currently requires parsing English. reason_code fixes only that.
--
-- source distinguishes a RECONSTRUCTED event (classified from historical prose)
-- from an OBSERVED one (written live at the moment of the status change). A
-- backfilled row looks just as authoritative as a live one and is not.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_events (
    id            INTEGER PRIMARY KEY,
    strategy_id   TEXT    NOT NULL,
    occurred_at   TEXT    NOT NULL,
    old_status    TEXT,
    new_status    TEXT,
    reason_code   TEXT    NOT NULL,        -- enum; UNCLASSIFIED is a finding, not a default
    reason_prose  TEXT,                    -- unchanged, never lossy
    source        TEXT    NOT NULL DEFAULT 'live',   -- 'live' | 'backfill'
    history_id    INTEGER,                 -- status_history.id when backfilled; NULL when live

    FOREIGN KEY (strategy_id) REFERENCES strategies(id)
);

CREATE INDEX IF NOT EXISTS idx_events_sid_at
    ON strategy_events (strategy_id, occurred_at);

-- the point of the whole table: causes become groupable
CREATE INDEX IF NOT EXISTS idx_events_reason
    ON strategy_events (reason_code);

-- Makes the backfill idempotent: re-running it cannot duplicate a row, because
-- each backfilled event is pinned to exactly one status_history row. Live rows
-- carry history_id NULL, and SQLite treats NULLs as distinct in a UNIQUE index,
-- so this constrains the backfill without constraining live writes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_history_id
    ON strategy_events (history_id);

-- ---------------------------------------------------------------------------
-- sleeve_equity — per-sleeve P&L per bar. CREATED EMPTY TODAY.
--
-- The writer is deliberately deferred: it touches live_test.py's hot loop, which
-- means restarting all 55 running sleeve processes. The schema lands now so its
-- shape is settled while the design is fresh.
--
-- This exists because per-sleeve live return is currently UNANSWERABLE at any
-- schema. live_test.py:1090 appends self.account_equity — the WHOLE OANDA account
-- balance — identically into all 27 sleeves, so the hourly report showing the same
-- figure for EUR_JPY, WHEAT_USD, EUR_GBP, USD_CHF and BTC_USD is one balance
-- copied 27 times, not 27 sleeve P&Ls.
--
-- sleeve_pnl must therefore be derived from the SLEEVE's own units x price move,
-- never from account equity. And it is append-only so that a restart cannot
-- truncate it the way equity_curve is truncated today (initialised to [] at
-- live_test.py:400, never loaded back, then capped at 365 entries at :1096).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sleeve_equity (
    id          INTEGER PRIMARY KEY,
    sleeve_id   TEXT    NOT NULL,
    bar_time    TEXT    NOT NULL,          -- the BAR's time, not wall-clock
    own_units   REAL,                      -- the sleeve's own units, NOT the account
    price       REAL,
    sleeve_pnl  REAL,                      -- units x price move. NOT account equity.
    written_at  TEXT,

    UNIQUE (sleeve_id, bar_time)           -- append-only + safe replay
);

CREATE INDEX IF NOT EXISTS idx_sleeve_equity_sid_bar
    ON sleeve_equity (sleeve_id, bar_time);

COMMIT;
