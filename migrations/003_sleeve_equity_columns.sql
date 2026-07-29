-- 003_sleeve_equity_columns.sql — widen sleeve_equity to record BOTH quantities.
--
-- The original spec stored only currency P&L (own_units x price move). That alone
-- is the wrong yardstick for "is this sleeve working as designed": currency P&L
-- CONFLATES EDGE WITH SIZING, so a sleeve that took a correlation haircut, a Kelly
-- downshift or a cluster-cap squeeze earns less without its strategy changing at all.
--
-- position_return (= position * bar_return) is SCALE-FREE and therefore immune to
-- every sizing decision, which makes it directly comparable to the reconstruction
-- incubation.py builds from the strategy's own code. Both columns are free: all the
-- inputs are already in scope at live_test.py:1280, where position_return is ALREADY
-- computed and fed to Kelly and the drawdown breaker — it was simply never persisted.
--
-- ALTER is DDL, so the append-only triggers do not block it (verified).
-- pipeline_utils.init_db applies these same ALTERs tolerantly for any other DB.

ALTER TABLE sleeve_equity ADD COLUMN position INTEGER;
ALTER TABLE sleeve_equity ADD COLUMN bar_return REAL;
ALTER TABLE sleeve_equity ADD COLUMN position_return REAL;
ALTER TABLE sleeve_equity ADD COLUMN source TEXT DEFAULT 'live';
