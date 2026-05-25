# Backlog — low-priority / deferred items

Items surfaced during the May 2026 review that were intentionally **not** fixed
because they are low-impact, need more data before deciding, or are larger
changes than the issue warrants. Blockers (B1–B5) and high-priority (H1–H8)
items from that review are already fixed and on `main`.

## Modeling / correctness

- **Backtest does not model the live stop-loss.** `compute_strategy_returns`
  is `signals.shift(1) * returns` — no stop. Live `_place_order` attaches an
  ATR `stopLossOnFill`. So the validated GT-score assumes the full signal is
  held; live can be stopped out early. The strategy that passes validation is
  not exactly the one that trades. Fixing it means simulating the ATR stop in
  the return calc, which changes every score — a deliberate decision, not a
  quick patch. *(pipeline_utils.py / validator.py)*

- **Regime-gate threshold over-tightening.** `grid_search` picks the
  highest-IS param combo, so it over-fits the regime-gate threshold (observed:
  `adx_thresh=30`) → zero out-of-sample windows. Decision pending: cap
  detector-threshold sweeps, or have `walk_forward` flag zero-window strategies
  with a clear reason instead of a generic sparse-trades failure. Wait for a
  full post-rotation batch before sizing the fix. *(pipeline_utils.py)*

- **Tighten edge-windows gate from ≥3/5 to ≥4/5.** The `wheatusd_…0524_…_i17`
  pass barely cleared the bar with per-window WF = `[0.0, 0.0, 0.238, 0.781,
  0.641]` — two windows completely dead, edge concentrated in the most recent
  two. Combined WF = 0.32 (barely above 0.30 gate). It IS a real strategy
  (torture flags empty, HO=0.51 on 192 trades) but the edge is clearly
  regime-dependent. Tightening to ≥4/5 would have rejected this kind of
  regime-fitter while still passing NZD-quality strategies (NZD had all 5
  windows with edge). Decision deferred — paper-deploying this strategy will
  give live data on whether regime continuity holds or breaks. If it breaks,
  that's the empirical case for tightening the gate. *(validator.py
  MIN_WINDOWS_WITH_EDGE)*

## Pipeline robustness

- **`DAILY_SWAP_RATE` coverage.** Only 4 instruments have swap rates; crypto
  perpetual funding and FX-cross swaps default to 0. Cost model understates
  carry for those. *(pipeline_utils.py)*

- **`record_validation` status mapping is fragile.** Maps `final_status` text
  to a status enum via substring matching (`'walk' in fl and 'forward' in fl`).
  A failure message with those words for an unrelated reason misclassifies.
  Use explicit failure codes. *(pipeline_utils.py)*

- **`wf_result['has_sufficient_windows']` direct dict access.** No `.get()` —
  a KeyError if `walk_forward`'s return shape ever changes. *(validator.py)*

- **`meta_review.call_llm` still shells out to the `claude` CLI.** Claude CLI
  was removed from code generation (commit 75bdcb8) in favour of OpenRouter;
  meta-review still uses it and silently falls back to rule-based directives if
  the CLI is absent. Move it to OpenRouter for consistency. *(meta_review.py)*

- **Thesis batch runs dry.** When `iteration > len(thesis_batch)` the loop
  falls back to slower single-shot thesis generation. Regenerate the batch when
  exhausted. *(auto_research.py)*

- **Five macro FRED series IDs are dead or discontinued.** During the macro
  backfill: `AUSCPIALLMINMEI` (au_cpi), `IRSTJPNM193N` (boj_rate) and
  `IRSTCB01AUM156N` (rba_rate) return HTTP 400 — the series IDs no longer
  exist on FRED. `BOERUKM` (boe_rate) stops in 2017 and `JPNCPIALLMINMEI`
  (jp_cpi) stops in 2022. So macro coverage is solid for US/EU/UK pairs but
  thin for AUD/JPY. Find current FRED series IDs for AU CPI, BoJ policy rate,
  RBA cash rate, and the current BoE rate. *(macro_fetcher.py col maps)*

- **`pnl_history` grows unbounded in the live trader.** `equity_curve` is
  trimmed to 365 entries; `pnl_history` is not. Harmless for daily strategies,
  ugly for long-running intraday ones. *(live_test.py)*

- **Telegram notify failures swallowed silently.** Several `notify_*` call
  sites catch and drop exceptions with no log line. *(live_test.py,
  auto_research.py)*

- **Torture tests use loop-bound `dev_data`.** `validate_strategy` passes the
  last timeframe's `dev_data` into `run_torture_tests`. Correct today (one
  timeframe per candidate) but brittle if multi-timeframe validation is ever
  re-enabled. *(validator.py)*

## Cleanup

- **`program.md` is vestigial.** Only a fallback for the commented-out
  `_build_system_prompt()` and a secondary research-directive fallback that
  `thesis.md` already serves. Safe to delete for a clean tree.

- **Redundant `get_failed_strategies()` call.** Called once before the loop and
  again inside each iteration. Trivial. *(auto_research.py)*

- **`status_history` table is written but never read.** Audit trail is
  populated on every status change but nothing consumes it — could power a
  "revert to last status" feature or failure-trend analysis. *(pipeline_utils.py)*

- **`exec()` of LLM-generated code, no sandbox.** Acceptable for a single-user
  personal bot. Revisit only if the pipeline ever runs untrusted or multi-user
  code. *(validator.py, live_test.py, auto_research.py)*

## Deferred enhancements

- **Persistence / hysteresis regime detector.** A 2-state regime classifier
  with sticky transitions (state must hold N bars before flipping) would reduce
  gate whipsaw. Add only if a batch shows the symptom — strategies with choppy,
  alternating per-window WF scores. Don't add speculatively.

- **Allow a 3rd entry condition (2 signal + 1 regime) for D/W strategies.**
  Currently entry is capped at 2 AND conditions and the mandatory regime gate
  counts as one — so the entry signal is squeezed to a single condition.
  Reasonable for D/W (enough bars to absorb the density hit). Decided to leave
  at 2 for now; revisit if strategies need a genuine two-part entry trigger.

- **Asset-class hint lands weakly on commodities and crypto.** First 20-iter
  batch (`forever_20260523_102033.log`) covered all 20 instruments. Result on
  iter 11–20 (XAG / oil / grains / crypto): **1 clear adoption** (WTI–Brent
  spread) / **2 partial** (NATGAS Mondays, soybean "weather") / **7 generic**
  rationales that fall back to FX-style price-action or generic US-macro
  framing. Notable misses: BTC ignored perp-funding & ETF flows; ETH ignored
  ETH/BTC ratio; CORN/WHEAT ignored USDA reports & harvest seasonality; NATGAS
  ignored winter heating. FX side (iter 1–10) had ~3/10 adoption — also weak.
  Likely cause: free LLMs default to abundant FX/equity training priors on
  assets they've seen less; a one-line note isn't strong enough to override.

  Three options to revisit (decided to observe steady-state first, not layer
  on yet):
    1. **Prescriptive hint** — for commodity/crypto instruments, escalate the
       hint to a mandatory clause like the macro constraint (e.g. NATGAS entry
       MUST reference a winter-season or storage signal).
    2. **Stronger model on these slots only** — route just the commodity/crypto
       code-gen calls to paid DeepSeek V3; FX stays on free models. Cents per
       batch, isolates the spend to where prior-overriding actually matters.
    3. **Accept the limitation** — let the validator filter the noise;
       recognise free LLMs won't produce genuine commodity/crypto edges without
       much more prompting work.

  Wait for ~1 week of 20-iter batches to confirm the pattern is consistent
  before picking. *(auto_research.py `_ASSET_HINTS` / thesis prompts)*
