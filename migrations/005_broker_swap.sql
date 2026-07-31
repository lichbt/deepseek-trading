-- 005_broker_swap.sql — record the swap the BROKER actually charged.
--
-- WHY THIS EXISTS: every swap figure the repo reasons about (weekend-flat costing,
-- the ~20%/yr NAS100 drag, the .t swap-free comparison) came from the published
-- per-symbol rate run through a formula. Only ONE accrual had ever been observed
-- (EUR_GBP, -0.09 USD on 0.01 lot, 2026-07-28). A rate that is published is not a
-- rate that was charged, and no backtest in this repo models swap at all — so the
-- live book runs below its simulated curve by an amount nothing measures.
--
-- WHAT A ROW IS: one observation of one open position's ACCRUED-TO-DATE swap, read
-- from ProtoOAReconcileReq. It is a running total, NOT a per-period charge — the
-- charge is the DELTA between two observations of the same position_id. That is why
-- observed_at is part of the key and why nothing here is ever updated in place.
--
-- APPEND-ONLY, sealed below, for the same reason as the other lifecycle tables: the
-- value of the series is the delta, and a writer that overwrites an earlier reading
-- destroys exactly the quantity being measured.
--
-- READ-ONLY AT SOURCE: the writer places no orders and amends nothing. It runs on
-- the Mac against the prop account deliberately — reading accrued swap needs no pod
-- push, so it is not a deploy and there is nothing for reset-db to destroy.

BEGIN;

CREATE TABLE IF NOT EXISTS broker_swap (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,       -- UTC ISO, when the reconcile was read
    position_id TEXT NOT NULL,
    instrument TEXT,                 -- repo name (EUR_GBP), NULL if symbol unmapped
    symbol_id INTEGER,
    side TEXT,
    volume INTEGER,                  -- WIRE volume (centi-units), as the broker reports it
    units REAL,                      -- volume / 100, the repo's unit convention
    entry_price REAL,
    swap_raw INTEGER,                -- as sent, before moneyDigits scaling
    money_digits INTEGER,
    swap_usd REAL,                   -- swap_raw / 10^money_digits: ACCRUED TO DATE, not a delta
    commission_usd REAL,
    opened_at TEXT,                  -- position open timestamp, UTC ISO
    UNIQUE (position_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_broker_swap_pos ON broker_swap (position_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_broker_swap_inst ON broker_swap (instrument, observed_at);

CREATE TRIGGER IF NOT EXISTS broker_swap_no_update
BEFORE UPDATE ON broker_swap
BEGIN
    SELECT RAISE(ABORT, 'broker_swap is append-only: UPDATE refused. Append a new observation instead.');
END;

CREATE TRIGGER IF NOT EXISTS broker_swap_no_delete
BEFORE DELETE ON broker_swap
BEGIN
    SELECT RAISE(ABORT, 'broker_swap is append-only: DELETE refused.');
END;

COMMIT;
