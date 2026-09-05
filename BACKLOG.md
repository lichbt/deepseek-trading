# Backlog — low-priority / deferred items

Items surfaced during the May 2026 review that were intentionally **not** fixed
because they are low-impact, need more data before deciding, or are larger
changes than the issue warrants. Blockers (B1–B5) and high-priority (H1–H8)
items from that review are already fixed and on `main`.

## Modeling / correctness

- **[DONE 2026-05-28] Backtest now models the live stop-loss.** Previously
  `compute_strategy_returns` was `signals.shift(1) * returns` with no stop while
  live `_place_order` attached an ATR `stopLossOnFill`, so validation scored a
  different return stream than what trades. Fixed (commit on `model-live-stop`,
  merged `fce9d7e`): `compute_returns_with_stop()` models the live stop
  (entry ± mult·ATR, intrabar trigger, flat-until-signal-changes);
  `compute_net_strategy_returns(params=...)` applies it across grid_search /
  walk_forward / holdout / torture; a coarse `stop_mult` sweep `[3,2,1.5]` is
  auto-injected into the SEARCH grid only (fingerprint stays on the original
  grid → dedup safe). Quantified gap on deployed strategies: WF moved 0.20–0.30.
  Re-validation of the 3 live strategies under the stop: NZD passes (HO 0.60),
  WHEAT 071105 near-miss (WF 0.288, HO 0.47, clean torture — kept in paper),
  WHEAT 032154 fails HO (−1.9%, retired). *(pipeline_utils.py / validator.py)*

- **[FINDING 2026-05-28] The ATR stop does NOT rescue holdout failures.** All
  60 historical near-misses that passed IS+WF but failed later were re-run
  through the stop-aware validator: **0 rescued.** 30 still fail HO decay
  (their holdout returns are genuinely negative — a stop caps losses but can't
  create edge), and 20 that previously passed IS/WF now fail those gates
  because the stop *lowered* their scores. Conclusion: the validator was not
  producing false negatives on this pool, and modeling the stop makes the gate
  strictly harder (correctly). No action — recorded so we don't re-run this
  experiment.

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

- **[DONE 2026-09-05] `zeabur_interlock.sh` no longer truncates remote output.**
  `remote()` ended with `puts $expect_out(buffer)`, and expect's `match_max`
  defaults to 2000 **bytes** with the overflow discarded from the FRONT — so any
  verb whose output exceeded ~2KB silently lost its head while still looking like
  a complete answer. Two misreads in one session: `state` returned unparseable
  JSON missing 6 of 18 sleeve entries, which made two correctly-owned positions
  (`4802715` xauusd_i5, `4802717` nas100usd_i1) read as unclaimed orphans and
  nearly got them flattened on the funded account; and `logs 100000` returned 44
  lines, which reads as log rotation rather than truncation and hid an entire
  weekend-flat/roll-flat failure sequence. Fixed with `match_max 2000000` placed
  AFTER the `spawn` line — before `spawn` it applies to no spawn id and is a
  no-op. Treat any large interlock read quoted in an older note as suspect.
  *(scripts/zeabur_interlock.sh)*

- **The cTrader client cannot recover from access-token expiry.**
  `_refresh_if_stale()` is reachable from exactly one place, `_on_connected()`,
  so a long-lived `fix_runner` re-checks token expiry only when the TRANSPORT
  reconnects. On 2026-09-04 the token expired in place and the pod was dead to
  the broker for ~9h: both the weekend-flat and roll-flat windows fired and
  closed 0 (every close failed on `stop cancel unconfirmed`, which is the
  correct post-2026-08-10 refusal), the 00:15 UTC pass failed in 1s, and the
  pod's prop guard was blind the whole time. `_authed` stays set, so `send()`
  never raises its own error — the failure only ever surfaces as a server
  rejection, and **nothing alerts**; it was found only because a human noticed
  weekend-flat had not fired. Compounding it, whichever host refreshes first
  ROTATES the refresh token, so a stranded pod cannot self-heal by restarting —
  it must be handed the current `.ctrader_tokens.json` blob. Fix: force a
  reconnect + re-auth on `OA_AUTH_TOKEN_EXPIRED` / `Trading account is not
  authorized` instead of trusting `_authed`, or add a periodic staleness check.
  Recurs ~every 30 days; next expiry 2026-10-05 03:06 UTC.
  *(ctrader_client.py:175 `_refresh_if_stale`, :319 `_on_connected`, :397 `send`)*

- **Candle fetch has no network timeout → can hang the pipeline.** During the
  2026-05-28 stop-loss re-validation, a `get_candles_date_range` call on
  `GBP_JPY` blocked indefinitely (0% CPU, no progress) and froze the whole
  batch — only a subprocess-per-strategy wrapper with a 240s ceiling let it
  proceed. A single hung fetch in `validate_strategy` or the auto-research loop
  would stall the live pipeline the same way. Add an explicit `requests`
  timeout (+ retry) to the OANDA candle fetch. *(data_fetcher.py)*

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

- **Five macro FRED series IDs are dead or discontinued.** *(PARTIALLY DONE
  2026-07-02)* The three HTTP-400 IDs (`AUSCPIALLMINMEI`/au_cpi,
  `IRSTJPNM193N`/boj_rate, `IRSTCB01AUM156N`/rba_rate) are REMOVED from the
  col maps (they had zero cached rows — always-NaN columns + uncacheable
  refetch churn in every validation window), and failed/empty fetches are now
  negative-cached in fred_meta until CACHE_MAX_AGE_DAYS. STILL OPEN:
  `BOERUKM` (boe_rate, frozen 2017) and `JPNCPIALLMINMEI` (jp_cpi, frozen
  2022-04) remain mapped — they cover dev-window history but forward-fill
  stale values in holdout/live; find replacement FRED IDs for AU CPI, BoJ
  rate, RBA rate, BoE rate, JP CPI. *(macro_fetcher.py col maps)*

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

- **The netting ledger drops its rounding residue, and an exit can under-close.**
  `sleeve_units.units` is a float; order units are formatted to the instrument's
  precision at send time (`f'{units:.{unit_precision}f}'`, live_test.py:1229 —
  whole units for everything except BTC/ETH/LTC). On an exit the sleeve writes
  its own units to exactly `0.0` while sending the *rounded* delta, so any
  fraction is silently abandoned at the broker and no sleeve owns it afterwards.
  **Observed 2026-08-28:** `xagusd_auto_20260719_072203_i16` filled 167 units on
  08-24 and exited -166 on 08-27, leaving a 1-unit long that the ledger read as
  flat. It survived a full pass and was closed by hand (order 24013, -$0.57).
  The residue is self-perpetuating: with the ledger at 0.0 the sleeve's next
  entry orders its full target, so the broker sits one unit above it on a long
  and one below on a short, permanently. Sub-unit drift on AUD_USD (-0.45),
  XAU_USD (-0.21) and XCU_USD (+0.29) is the same effect below the threshold —
  harmless only because it never rounds past a whole unit.
  Fix is to carry the residue rather than zero it: record what was actually
  SENT, not the target, so the next order includes the unsent fraction. Touches
  live ordering, so it wants its own change plus a reconciliation check
  (ledger-vs-broker per instrument, counting `incubating` sleeves — filtering to
  `paper_trading` alone reads every incubating position as a 50k orphan).
  *(live_test.py `netting_delta` / `_save_own_units` / `_place_order`)*

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

- **Crypto backfill: revisit only if 2-year OANDA window yields zero passes.**
  OANDA's practice account didn't carry crypto until 2019 (BTC) / 2020 (ETH,
  LTC), so the validator's DEV window for crypto is now per-instrument and
  only 2 years long (vs 5 for FX). External sources cover the missing
  2015-2019 — CryptoCompare back to 2010 (BTC), 2015 (ETH), 2013 (LTC);
  Binance/Bitstamp/Coinbase have varying coverage. But all of them have a
  **basis mismatch with OANDA**: OANDA quotes crypto as a CFD with
  30-100 USD spread; spot venues quote at 0.5-2 USD. A strategy that passes
  on Binance prices may be unprofitable on OANDA because the per-trade cost
  is ~30-100× higher. Naïve backfill produces optimistic-biased validation.

  Three honest options if/when we revisit:
    1. **Backfill from CryptoCompare unadjusted** — fastest (~1-2 days), but
       optimistic-biased; more false positives that fail forward test.
    2. **Stitch (external pre-2019 + OANDA post-2019)** — regime discontinuity
       at the 2019 boundary (right at COVID) corrupts walk-forward windows.
       Worse than (1).
    3. **Basis-adjusted backfill** — fit a spread adjustment from the OANDA
       data we DO have, apply it to external prices. Most honest; ~1 week of
       work plus ongoing maintenance.

  **Decision rule:** if 2-4 weeks of running with the per-instrument DEV
  window produces ZERO crypto passes, the 2-year window is genuinely too
  short and we revisit with option (3). If even one crypto strategy passes,
  the window was enough — don't bother. *(validator.py DEV_OVERRIDES,
  data_fetcher.py would need a new path)*

- **Portfolio pool size: extend beyond ~15 toward ~50 strategies? — DEFERRED, revisit later.**
  Question (2026-06-07): is it good to grow the live book to ~50 strategies if
  they're uncorrelated? There is NO hard cap in code — `portfolio.py` weights
  every `paper_trading` row and `run_paper_trading.sh` spawns one trader each,
  so the count is purely a deployment decision (currently 14 live).

  Conceptual answer: YES in principle — more genuinely-uncorrelated, real-edge
  sleeves raise portfolio Sharpe (~sqrt(N)) and let us hit the prop +10% target
  while staying under the 5% daily / 10% total drawdown limits. Direction is right.

  Three caveats that matter more than the number:
    1. **Quality is the binding constraint, not the cap.** Pipeline yields ~1
       genuinely good strategy per several batches; most passes are weak/overfit/
       redundant. Filling 50 slots means lowering the bar → deploying no-edge
       sleeves that add operational + drawdown risk for no return. Don't chase a count.
    2. **True independence is finite.** 50 strategies over ~15-20 instruments =
       several per instrument (cf. the 4 NZD now). Low return-correlation still
       shares instrument/regime risk. Real diversification needs instrument
       BREADTH + different drivers — the actual lever is more instruments with
       edge, not more strategies on the same handful.
    3. **Prop sizing must scale down as N grows.** Because weight_scale =
       own_weight * n normalises each sleeve to ~baseline risk regardless of N,
       50 strategies ≈ 3x the aggregate risk of 15; portfolio DD grows ~sqrt(50/15)
       ≈ 1.8x. To stay under 5%/10% we'd LOWER RISK_PER_TRADE (e.g. 0.5% → ~0.27%)
       as the book grows. The diversification then shows up as higher Sharpe at
       the same DD (the desired outcome).

  **Decision rule:** don't target a fixed count. Keep the quality bar (real WF
  edge, low DD, low correlation / NEW instrument) and let the book grow naturally
  toward ~20-30 as quality uncorrelated candidates appear. When it grows
  meaningfully, drop RISK_PER_TRADE to keep aggregate DD within prop limits.
  Optionally build a "soft ceiling N + auto-retire weakest when a stronger
  uncorrelated one passes" manager — a quality-management feature, not a
  raise-the-number one. Revisit when (a) we have >~25 quality candidates queued,
  or (b) we add materially more instruments with genuine edge.
  *(portfolio.py load_strategies, live_test.py RISK_PER_TRADE / weight_scale)*
