-- 004: free-text note on an evaluation row.
--
-- `verdict` is machine-generated ("LOOKAHEAD=PASS DECAY=INSUFFICIENT") and says
-- what the gates measured, never WHY a candidate was accepted or rejected. The
-- judgement — near-duplicate of an incumbent, return is one historical year,
-- instrument unroutable at the venue — lived only in a chat transcript and was
-- lost the moment the session ended.
--
-- Skip/retire reasons already survive in status_history.reason and
-- strategy_events.reason_prose; this is the missing third case, an evaluation
-- that did NOT change status but still reached a conclusion worth keeping.
--
-- evaluations is APPEND-ONLY (sealed against UPDATE/DELETE by 002_seal.sql), so
-- a note is written at INSERT time and is immutable afterwards. Annotating an
-- earlier evaluation means recording a NEW evaluation, which is the correct
-- semantics: a later opinion is a later observation, not an edit of the old one.
--
-- ALTER is DDL, so the append-only triggers do not block it.

ALTER TABLE evaluations ADD COLUMN notes TEXT;
