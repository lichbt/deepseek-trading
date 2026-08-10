# Generic chain-head A/B controller

Type: task
Status: open
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
