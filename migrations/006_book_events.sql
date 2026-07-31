-- Book-level watch findings: a daily loss worth looking at, and a sleeve that
-- has stopped evaluating bars.
--
-- WHY A NEW TABLE rather than strategy_events. A book-wide daily loss has no
-- strategy_id, and strategy_events declares `strategy_id TEXT NOT NULL` with a
-- FOREIGN KEY into strategies(id) — a sentinel like '__book__' would be a lie
-- that breaks the moment PRAGMA foreign_keys is ever turned on.
--
-- DEDUP IS STRUCTURAL, NOT STRING-MATCHED, and the key choice is the whole
-- design: bar_time is THE BAR THE FINDING IS ABOUT, not the time of the run.
--   * BOOK_LOSS      -> the losing bar. One alert per bad day, however often
--                       the watcher runs.
--   * SLEEVE_STALE   -> the sleeve's LAST OBSERVED bar, which does not change
--                       while the sleeve is stuck. So one alert per STALL
--                       EPISODE, not one per bar as the gap widens. A later,
--                       separate stall has a different last-observed bar and
--                       therefore alerts again.
--   * SLEEVE_RESUMED -> the same last-stale bar, so a recovery is announced
--                       once and pairs with its own STALE row.
-- INSERT OR IGNORE therefore makes a re-run a no-op, the same property
-- sleeve_equity gets from UNIQUE (sleeve_id, bar_time).
--
-- sleeve_id is NOT NULL DEFAULT '' rather than nullable BECAUSE NULLs COMPARE
-- DISTINCT IN SQLITE: a nullable column would defeat the UNIQUE constraint for
-- every book-level row and re-alert forever.
--
-- Append-only like the other lifecycle tables. Sealed at the end of this file;
-- the triggers block UPDATE/DELETE but not INSERT.

CREATE TABLE IF NOT EXISTS book_events (
    id          INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,              -- when the watcher observed it
    event_code  TEXT NOT NULL,              -- BOOK_LOSS | SLEEVE_STALE | SLEEVE_RESUMED
    sleeve_id   TEXT NOT NULL DEFAULT '',   -- '' for book-level findings
    bar_time    TEXT NOT NULL,              -- the bar the finding is ABOUT
    detail      TEXT NOT NULL,
    UNIQUE (event_code, sleeve_id, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_book_events_code ON book_events (event_code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_book_events_sleeve ON book_events (sleeve_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS book_events_no_update BEFORE UPDATE ON book_events
BEGIN SELECT RAISE(ABORT, 'book_events is append-only: UPDATE refused. Append a correcting event instead.'); END;
CREATE TRIGGER IF NOT EXISTS book_events_no_delete BEFORE DELETE ON book_events
BEGIN SELECT RAISE(ABORT, 'book_events is append-only: DELETE refused.'); END;
