# Trials-accounting + failure-taxonomy wiring — COMPLETED, with one instruction REVERSED

> **STATUS 2026-08-22 — this is a finished work order kept as a record, NOT a task.**
> All three wiring points landed. Do not re-run it. One instruction below
> (wiring point 2's DSR gate) was DELIBERATELY REVERSED after it shipped, and
> following this file as written would re-introduce a rejected behaviour.
>
> **Live gate values are NOT in this file.** They are generated from `validator.py`
> into [ARCHITECTURE.md](ARCHITECTURE.md#validation-gates) by
> `scripts/sync_gate_docs.py`. This file predates that and its numbers are frozen
> at what was proposed, not what runs.
>
> | wiring point | status | where |
> |---|---|---|
> | 1. log every trial | **DONE** | `validator.py:99` calls `H.record_trial` |
> | 2. DSR gate at meta-review | **DONE but OBSERVE-ONLY — the reject was reversed** | `validator.py:157` computes it; `DSR_GATE_ENABLED` defaults OFF |
> | 3. taxonomy feedback | **PARTIAL** | tagging is live via `validator._failure_tag`; `classify_failure` and `failure_feedback` are NOT called outside `strategy_honesty.py`'s own demo, and nothing prepends feedback to the generator prompt |
>
> **Why wiring point 2 was reversed (binding — do not relitigate).** The brief says
> `if dsr < 0.95: reject`. The code deliberately does not. DSR deflates *Sharpe*
> while the pipeline selects on *GT-score* — a mismatched axis, so DSR runs
> systematically low for any GT-selected winner and a low value does not mean
> overfit. Overfit is controlled by the LOCKED HOLDOUT, not DSR. `validator.py:65`
> carries the warning in full: *"DO NOT set DSR_GATE=1 as-is — it would hard-reject
> valid GT-selected strategies on a mismatched axis; re-aim it at the selection
> metric (deflate GT-score, not Sharpe) before ever gating on it."* Landed as
> commit `f1933dd`, "DSR: relabel as descriptive (deflation axis != selection axis)".
>
> So the `dsr < 0.95` and `dsr >= 0.95` lines below are the ORIGINAL PROPOSAL and
> are not live behaviour. `DSR_MIN = 0.95` does exist in `validator.py`, but it is
> descriptive and only bites if someone sets `DSR_GATE=1`, which the above forbids.
>
> Note the brief's own rule at wiring point 3 — *"reuse the same constants. Do not
> invent new numbers."* — is exactly right, and the 0.95 in wiring point 2 breaks
> it. That is the one number this file invented, and it is the one that had to be
> walked back.

---

## Original work order (kept verbatim below)

# Task: wire trials-accounting + failure-taxonomy into the strategy loop

Module is already in the repo: `strategy_honesty.py`. Do NOT rewrite it.
Wire it into the existing generation → validation → meta-review loop at three
points. When done, run the self-check at the bottom and report pass/fail per item.

## Context (do not break these)
- File-first, resumable. SQLite for dedup/time-series. 9router for LLM calls.
- Telegram for notifications. Mock-default adapters — must still run offline.
- Per-period returns are DAILY. Feed Sharpes in per-period (non-annualised) units.

## Wiring point 1 — log EVERY trial (not just winners)
In the loop, after each strategy is validated, call:
    record_trial(db, strat_hash, sharpe, passed_wf, passed_ho, failure, meta)
- `sharpe` = the candidate's per-period (daily) Sharpe.
- Losers MUST be logged too — they define the variance the DSR correction needs.
- This replaces nothing; it's an add. Keep existing dedup as-is.

## Wiring point 2 — DSR gate at the meta-review checkpoint
Before promoting any HO-passer at the ~30-strategy meta-review:
    dsr = deflated_sharpe_ratio(cand_daily_returns, trial_sharpes(db))
    if dsr < 0.95: reject as failure_tag="ho_decay"; do NOT promote.
Also compute trials_per_ho_pass(db) each checkpoint and send it to Telegram.

## Wiring point 3 — taxonomy feedback into generation
- In validation, build a ValidationResult per strategy; set failure_tag via
  classify_failure(folds, wf_min_folds=<OUR GATE>, dd_limit=<OUR GATE>, ho_passed=...).
- Replace the stub thresholds with our real WF/DD gates — find them in the
  existing validator and reuse the same constants. Do not invent new numbers.
- At generation time, prepend failure_feedback(recent_results) to the instruction
  prompt that goes to the generator.

## Stop / budget (the loop must terminate)
- Stop when: a candidate clears WF AND HO AND dsr >= 0.95  → Telegram + halt.
- OR strategies_tried >= MAX_TRIALS (config) → Telegram summary + halt.
- One FINAL locked holdout the loop never touches: only the single winner gets
  one shot at it, at the very end. If that file is read anywhere inside the loop,
  that's a bug — flag it.

## Self-check — report each as PASS/FAIL, do not skip
1. `python strategy_honesty.py` still runs (sanity).
2. Run loop with mock adapters offline: trials table populates with BOTH pass
   and fail rows.
3. Force a known-overfit candidate: confirm it clears HO but DSR < 0.95 and is
   rejected as ho_decay.
4. Telegram fires once on halt with: winner (or none), trials_tried,
   trials_per_ho_pass, final DSR.
5. Grep the loop: the final-locked-holdout path appears exactly ONCE, outside
   the loop body.

Report the five results, then stop. Do not refactor unrelated code.
