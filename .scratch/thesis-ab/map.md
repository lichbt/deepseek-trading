# A/B capability for LLM chain heads

Labels: `wayfinder:map`

## Destination

A **reusable A/B harness** that can swap the head of any model chain
(`THESIS_MODELS` / `CODEGEN_MODELS` / `CRITIQUE_MODELS`), tag every candidate it
produces with its arm, and support a pre-registered verdict — **exercised end to end on
exactly one question**: does `byteplus:deepseek-v4-pro` beat `byteplus:deepseek-v4-flash-ga-260731`
as the thesis head?

Done when the harness exists, the thesis experiment has run to its pre-registered n, and
the verdict is recorded in the Second Brain.

## Notes

**Domain:** automated strategy generation (`auto_research.py`), validated into
`pipeline.db`. Read `CLAUDE.md` and the boot-loaded `DECISIONS.md` before any session.

**This map carries EXECUTION, not just decisions** (overrides Wayfinder's plan-only
default, agreed at charting). `task` tickets build and run things.

**Skills:** `/grilling` + `/domain-modeling` for any ticket that reopens a design choice.

### Settled during charting — do not relitigate

| # | Decision |
|---|---|
| Arms | control `byteplus:deepseek-v4-flash-ga-260731` · challenger `byteplus:deepseek-v4-pro` |
| Primary endpoint | **WF non-zero rate** (`walk_forward_gt_score != 0`), currently 12.9% |
| Secondary | rank test (Mann-Whitney) on the non-zero tail; IS non-zero rate |
| Effect size | detect **+100% relative**, α=.05 two-sided, 80% power |
| Sample size | **147 per arm** (~10.4h wall clock for both arms) |
| Balance | by **count**, not wall-clock — pro is ~1.8x slower per candidate |
| Pairing | alternate arms on **consecutive batches sharing a seed** (`_asset_mode_for(seed)`) |
| Arm tagging | **JSONL sidecar keyed by explicit `strategy_id`** — never inferred from `created_at` |
| Sidecar durability | gitignored during the run, **committed once at the end** with the result |
| Missing tags | excluded **and counted in the report** — never silently dropped |
| Decision rule | **asymmetric**: pro must WIN the primary to be adopted. Tie or loss → keep flash-ga, on its 1.8x throughput advantage |
| Verdict record | Second Brain decision **either way**; `.env` edited only if pro wins |
| Venue | the **live production loop**, on the branch actually running. Both arms produce real candidates |
| Genericity | head-swap is chain-generic from day one; the outcome metric is thesis-specific |
| Rule ↔ n coupling | n=147 is valid ONLY under the asymmetric rule. Soften the rule and n must rise (510/arm for +50%). The analysis script reads both from the pre-registration so they cannot drift apart |
| Provenance | every sidecar row stamps git sha + branch + the model id actually sent; the analysis refuses a verdict if the sha moved mid-run |

### Live-loop facts (verified 2026-08-10)

Research loop = pid 3530, launchd `com.lich.autoresearch`, running
`/Users/lich/deepseek-oanda-trading/auto_research.py --max-iter 20 --target 20` from the
**working tree** on `feat/academic-recall-category`. Single loop — the second
`run_forever.sh` pid is its child, not a duplicate (two loops would race on
`.ab_test/counter` and corrupt arm alternation).

### Measured facts this design rests on (2026-08-10)

- `validation_results` n=82,234 · WF μ 0.0403 σ 0.1522 · **87% exact zeros**
- WF non-zero 12.9% · IS non-zero 60.3% · pass-at-validation 0.224%
- Median end-to-end **91s/candidate**; thesis gen ~51s (flash) vs ~123s (pro)
- Existing arm-alternating controller: `auto_research.py:368`, ledger `.ab_test/ledger.jsonl`

### Prior art — this matchup already ran once

2026-08-04, n=32/arm, flash (pre-GA `-260425`) vs pro: validity 30/32 vs 28/32, "inside
noise", latency 51s vs 123s. Conclusion was *equal quality, 2.4x faster*. **Prior
expectation is therefore NO difference** — this effort is that question properly powered,
with a harder endpoint, against the newer GA build.

## Decisions so far

<!-- one line per closed ticket -->

_(none yet — charting session only)_

## Not yet specified

- **Did the pairing actually help?** Post-hoc, the paired correlation can be measured from
  the run's own data and would tell every future A/B whether to bother. Can't be specified
  until data exists.
- **Mid-run arm imbalance.** If the sidecar shows the arms diverging badly in count (a
  crash, a stall on one model), what's the repair — extend the short arm, or void and
  restart? Depends on how the harness actually behaves under failure.
- **Promotion mechanics if pro wins.** The `.env` edit is trivial, but pro at 1.8x slower
  changes daily candidate throughput, which feeds every downstream cadence assumption.
  Unspecifiable until we know whether it wins.

## Out of scope

- **Running a codegen or critique A/B.** The harness must *support* it (Destination), but
  this effort tests thesis only. A second customer is a fresh effort.
- **The `thinking: {"type": "disabled"}` arm.** Measured 2026-08-10 as a real lever
  (reasoning 0 chars, ~1.7s vs ~2.7s), but folding it in makes a 2×2 the throughput
  can't resolve. Separate A/B, later.
- **Any change to the validation gates.** The endpoint is measured through the gates as
  they stand; moving them mid-effort would invalidate the comparison.
- **Pass-at-validation as an endpoint.** Ruled out on arithmetic: 10,476/arm to detect a
  doubling at p=0.224%, i.e. ~34 days per arm. Permanently unreachable at this throughput.
