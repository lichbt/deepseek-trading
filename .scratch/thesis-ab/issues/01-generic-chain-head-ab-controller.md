# Generic chain-head A/B controller

Type: task
Status: resolved
Blocked by: —

## Question

Build the reusable head-swap controller in `auto_research.py`, alongside the existing
`AB_TEST_FINGERPRINT` one (`auto_research.py:368`) rather than replacing it.

Requirements settled at charting:

- **Chain-generic.** `AB_TEST_CHAIN=thesis|codegen|critique` selects which chain's head is
  swapped. All three already parse through `_parse_model_chain`, so the swap is the same
  code path; only the outcome metric is thesis-specific.
- **Arms from env**, e.g. `AB_ARM_CONTROL` / `AB_ARM_CHALLENGER`, so the harness carries no
  hardcoded model id.
- **Alternate per batch**, reusing the persistent-counter + ledger pattern already proven.
- **Paired seeds**: consecutive batches form an A/B twin sharing one seed, passed through
  the existing `_asset_mode_for(seed)` hook, so each pair sees an identical
  instrument/constraint/timeframe schedule.
- **Sidecar tagging**: append one JSONL row per generated candidate recording an explicit
  `strategy_id`, plus chain, arm, batch, seed, and timestamp. The `strategy_id` is
  mandatory — arm must never be recoverable only by `created_at` inference, which is the
  defect in the existing fingerprint A/B.
- **Fail closed**: any error in the controller defaults the batch to the CONTROL arm and
  says so loudly. A silently mis-armed batch is worse than a lost batch.
- **Provenance stamp**: every sidecar row also carries the `git` sha and branch the loop
  was running from, and the resolved model id actually sent to the provider (not the
  configured one). The live loop runs the WORKING TREE from
  `/Users/lich/deepseek-oanda-trading` — verified 2026-08-10, pid 3530 under launchd
  `com.lich.autoresearch` — so a branch checkout or an edit mid-run silently changes the
  code generating the sample. Stamping converts that from a discipline problem into one
  the analysis can DETECT: if the sha moves mid-run, the run is compromised and the
  analysis must say so rather than averaging across two different codebases.

Deliberately NOT in this ticket: the stopping rule (declared in the pre-registration,
enforced during the run) and any analysis.

## Definition of done

Unit tests prove: arms alternate; twin batches share a seed; every generated candidate
appears in the sidecar with its `strategy_id`; a forced controller exception yields the
control arm. Paste real test output — a self-reported pass on unrun code does not count.

## Answer

Built in `auto_research.py`, alongside (not replacing) the `AB_TEST_FINGERPRINT`
controller. Env surface: `AB_TEST_CHAIN=thesis|codegen|critique`, `AB_ARM_CONTROL`,
`AB_ARM_CHALLENGER`. No model id is hardcoded.

**Components**

- `_ab_chain_arm()` — per-chain persistent counter (`.ab_test/counter-<chain>`), even
  batch = control, odd = challenger. Pins `_AB_PAIR_SEED = batch // 2`, so twins (0,1),
  (2,3)… share a seed. Appends to `.ab_test/ledger-<chain>.jsonl`.
- `_ab_apply_arm()` — moves the arm's model to the head of the named chain and keeps the
  derived `THESIS_MODEL` / `DEFAULT_MODEL` aliases consistent. Preserves the rest of the
  chain as fallback rather than truncating it.
- `_ab_tag_candidate()` — appends a row to `.ab_test/tags-<chain>.jsonl` with an explicit
  `strategy_id`, arm, model, batch, seed, instrument, timeframe, `git_sha`, `git_branch`.
- `_ab_git_provenance()` — best-effort sha/branch capture.
- `_asset_mode_for()` — one line: falls back to `_AB_PAIR_SEED` before the hourly bucket.

**Placement decisions**

- The tag is written AFTER `_validate_candidate`, so the sidecar holds exactly the
  population the analysis joins to `validation_results`. Candidates that die earlier
  (dedup, signal errors) never get a score and are correctly absent.
- `self.model` is dead for routing — thesis generation iterates `_chain_order(THESIS_MODELS)`
  — but `args.model` is captured before the swap and is PRINTED in the run banner. It is
  now corrected post-swap so the banner cannot mislead the dry run.

**Refusals (fail closed, all loud)** — unknown chain; missing arm models; counter failure
(→ control, `failed_closed: True`); `AB_TEST_FINGERPRINT` simultaneously active (two A/Bs
would confound each other).

**Evidence**

- `tests/test_ab_chain_head.py` — 16 tests: `16 passed in 0.97s`
- Full suite: `1148 passed, 11 warnings in 33.44s`
- Inactive path verified inert (the live loop re-reads this file every iteration):
  `_ab_chain_arm()` → None, `_AB_PAIR_SEED` → None, thesis head unchanged
  (`byteplus:deepseek-v4-flash-ga-260731`), hourly rotation intact, tag a no-op.

**Found while building** — a test-isolation defect of my own: `_ab_apply_arm` mutates
module globals by design (one batch per process in the real loop), which leaked into
`test_provider_pin.py` and failed 2 tests. The fixture now snapshots and restores every
global the controller can touch. Worth knowing for any future test that swaps a chain.

**NOT done here, by design** — the stopping rule (declared in 02, enforced in 04) and any
analysis. Nothing is enabled: `AB_TEST_CHAIN` is unset, so the controller is dormant.
