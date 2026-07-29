-- 002_seal.sql — make the lifecycle tables append-only.
--
-- RUN THIS ONLY AFTER the strategy_events backfill has been verified. Sealing
-- first would mean a botched 148,984-row backfill could only be repaired by
-- DROPping the very trigger that exists to stop history being rewritten.
--
-- WHY TRIGGERS AND NOT CONVENTION: the defect this store exists to fix was a
-- WRITER OVERWRITING HISTORY. live_test.py initialises equity_curve to [] at
-- :400, never loads it back from the DB, then writes the short in-memory buffer
-- over the stored JSON — silently truncating the record book-wide. Enforcing
-- append-only by convention would be enforcing it with the thing that already
-- failed once.
--
-- COST, ACCEPTED DELIBERATELY: a genuinely bad row can no longer be corrected in
-- place. The repair path is to append a correcting row, or to DROP the trigger,
-- fix, and re-seal — which is loud and deliberate, as intended.
--
-- NOT SEALED AGAINST INSERT. These tables are append-only, not read-only.

BEGIN;

-- evaluations ---------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS evaluations_no_update
BEFORE UPDATE ON evaluations
BEGIN
    SELECT RAISE(ABORT, 'evaluations is append-only: UPDATE refused. Append a new evaluation instead.');
END;

CREATE TRIGGER IF NOT EXISTS evaluations_no_delete
BEFORE DELETE ON evaluations
BEGIN
    SELECT RAISE(ABORT, 'evaluations is append-only: DELETE refused.');
END;

-- strategy_events -----------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS strategy_events_no_update
BEFORE UPDATE ON strategy_events
BEGIN
    SELECT RAISE(ABORT, 'strategy_events is append-only: UPDATE refused. Append a correcting event instead.');
END;

CREATE TRIGGER IF NOT EXISTS strategy_events_no_delete
BEFORE DELETE ON strategy_events
BEGIN
    SELECT RAISE(ABORT, 'strategy_events is append-only: DELETE refused.');
END;

-- sleeve_equity -------------------------------------------------------------
-- Sealed now even though it is empty: this is precisely the table whose
-- predecessor (live_status.equity_curve) was destroyed on every restart.
CREATE TRIGGER IF NOT EXISTS sleeve_equity_no_update
BEFORE UPDATE ON sleeve_equity
BEGIN
    SELECT RAISE(ABORT, 'sleeve_equity is append-only: UPDATE refused. A restart must never rewrite stored bars.');
END;

CREATE TRIGGER IF NOT EXISTS sleeve_equity_no_delete
BEFORE DELETE ON sleeve_equity
BEGIN
    SELECT RAISE(ABORT, 'sleeve_equity is append-only: DELETE refused.');
END;

COMMIT;
