# CLAUDE.md — deepseek-oanda-trading

## Binding decisions (loaded at session boot)

Rules already decided for this pipeline — honor them, don't relitigate. The
GT-Score gates, one-shot validation, and design constraints are load-bearing.
Generated from the Second Brain; to add one, use `brain.py decision` (below).

@/Users/lich/secondbrain/projects/deepseek-oanda-trading/DECISIONS.md

## Second Brain (durable decision context)

This repo is wired to the Second Brain at `/Users/lich/secondbrain`. Consult it
for durable decisions and cross-project knowledge — **not** code-level details,
which live in this repo's own docs (ARCHITECTURE.md, CONFIGURATION.md,
FILE_STRUCTURE.md, BACKLOG.md).

Query it before making research/validation/design decisions:

```bash
/Users/lich/secondbrain/.venv/bin/python /Users/lich/secondbrain/brain.py \
    query "<your question>" --project deepseek-oanda-trading --json
```

Add `--all` to check whether another project (e.g. trading-pipeline) solved it.

## Writing back to the brain

When a durable decision or constraint is made — a validation-gate change, a
design rule, a metric change, a broker/data choice, a direction change — save it
so future sessions honor it:

```bash
/Users/lich/secondbrain/.venv/bin/python /Users/lich/secondbrain/brain.py \
    decision --project deepseek-oanda-trading "decided X because Y"
```

Save durable decisions only — never code changes, candidate JSON, run results, or
`pipeline.db` state (those are this repo's own concern, not the brain's).

## Sleeve operations — always use the skill

**`.claude/skills/sleeve-ops/` is the entry point for evaluating, stress-testing,
deploying and retiring sleeves. Invoke it; do not re-derive any of this inline or
from memory.** Three modes over one pipeline, each with its own reference — read
only the one for the mode you're in:

| Ask | Mode | Reference |
|---|---|---|
| "check this strategy `<id>`", swap-or-add, retire-or-keep | evaluate | `references/evaluate.md` |
| prop sizing, DD odds, "can we scale up", pass probability | montecarlo | `references/montecarlo.md` |
| "deploy it", "retire `<id>`", "push to Zeabur" | deploy | `references/deploy.md` |

A full candidate review runs all three: evaluate → re-run montecarlo because the
sleeve count changed → deploy.

### Verifying a strategy (evaluate mode)

`references/evaluate.md` holds the 6-check lens, the swap / keep-both / reject
table, and **two reconstruction gotchas that have each produced a wrong deploy
decision**. That is the reason to invoke it rather than improvise a check.

- **Reconstruct, don't assert.** Every claim about a strategy's behaviour must come
  from a run made in this session — never from stored scores, a previous session, or
  this file.
- **Evaluation is read-only** and needs no confirmation. Deploy and retire need an
  explicit "yes" naming the sleeve.
- **`causal_audit.py` cannot answer deploy-or-not.** It takes `--sids` but iterates
  `status='paper_trading'` *before* filtering, so passing an undeployed candidate's
  id yields an empty run. It answers retire-or-keep for a live sleeve only.
- **Never quote a stale number.** Deployed risk, worst day, sleeve count and cluster
  caps all move whenever the book changes — recompute them (SKILL.md lists the
  commands) instead of carrying a value in.
- **Never pin an end date** for decay checks. `evaluate_strategy.FULL_END` was once
  hard-coded and silently scored later runs on stale data.

### FIX sleeve deployment

Full procedure: `.claude/skills/sleeve-ops/references/deploy.md`. Invoke the skill
rather than working from this summary.

1. Activate it as `paper_trading`.
2. **Restart both traders** — `com.lich.papertrading` *and* `com.lich.fixtrading`.
   Each freezes its sleeve list at process start, so until they restart a new sleeve
   never trades and a retired one keeps trading (and re-enters on its next flip).
3. Rebuild `portfolio_state.json`, then re-check deployed risk — cluster caps don't
   renormalise, so changing the sleeve count moves every remaining position.
4. Build a compact `pipeline.db` with `scripts/build_deploy_db.py --no-units`
   (`paper_trading` rows plus matching `validation_results` / `live_status`).
5. Never replace or commit the large local research `pipeline.db`.
6. Stage the compact DB from a temporary file with `git hash-object` and
   `git update-index --cacheinfo`, leaving the working DB untouched.
7. Commit only `pipeline.db`, `portfolio_state.json`, and `portfolio.py` when its
   conviction multiplier changed. Never stage unrelated working-tree changes.
   **Never commit `fix_runner_state.json`** — it is per-host live broker state, and
   a shipped copy makes a fresh volume claim positions it doesn't own.
8. Verify: FIX state `pos_id` count equals `broker_positions=N` in the log, and no
   resting stop order lacks a position. Repair mismatches from real PosIDs, never
   from a count.

**Production is the Mac, not Zeabur** (since 2026-07-27; the deployment is scaled to
0). Exactly **one `fix_runner` may run per broker account** — both hosts share
`FIX_LOGIN`, so two runners trade one account blind to each other and double the
effective size. While Zeabur is up, `git push` to `feature/ctrader-adapter` is a
**trading action**: it auto-deploys, and `fix_runner` runs a full trading pass on
startup. The mounted `/data/pipeline.db` is only seeded when absent, so no push ever
refreshes a live volume's book — delete the volume file to force it.
