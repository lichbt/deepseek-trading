# deploy — activate, publish, retire

**Requires an explicit "yes" naming the sleeve.** Never infer approval from a
positive evaluation.

## A. Activate on the paper book

```bash
source ~/.zshrc

# 1. DB only: status -> paper_trading, create the live_status row
./venv/bin/python -c "import pipeline_utils as p; p.start_live_trading('<id>')"

# 2. Restart the trader so it picks up the new sleeve
launchctl kickstart -k gui/$(id -u)/com.lich.papertrading

# 3. Recompute inverse-vol weights -> portfolio_state.json
bash run_portfolio.sh 2>&1 | tail -40
```

`run_portfolio.sh` sources `~/.zshrc` itself and runs `portfolio.py --write`. It
sources creds deliberately — without them a cold-cache run drops H4/H1 sleeves and
writes a *partial* `portfolio_state.json`. Check its output for dropped sleeves.

**Read the resulting `weight_scale`** in that output. `weight_scale = own_weight ×
n_strategies`; 1.0 = normal 0.5%-per-trade. Then:

- **Deploying small?** Add the sleeve to `portfolio.CONVICTION` (`sid` → multiplier,
  default 1.0) *before* step 3. It multiplies the inverse-vol weight and persists
  across rebalances.
- **Counter-intuitive:** a *low-vol* sleeve is *over*-weighted by inverse-vol, so it
  needs a *smaller* multiplier. Two sleeves at the same conviction land at different
  weight_scales purely from their vol. Set the multiplier by iterating on the
  observed weight_scale, not by picking a number that looks right.

Then confirm the book's magnitude didn't shift (see SKILL.md — cluster caps don't
renormalise), and re-run montecarlo mode plus `stress_book.py`.

The new sleeve incubates for ≥5 active days before it counts.

## B. Publish to Zeabur (FIX runtime)

The local `pipeline.db` is ~273 MB; the deployed one is ~170 KB. **The working DB is
never committed and never modified** — build a compact copy in a temp file and stage
*that blob* directly into the index.

```bash
# 1. Build the compact DB (paper_trading rows only) into a TEMP path
./venv/bin/python scripts/build_deploy_db.py --out /tmp/deploy.db

# 2. Stage the blob without touching the working file
BLOB=$(git hash-object -w /tmp/deploy.db)
git update-index --cacheinfo 100644,"$BLOB",pipeline.db

# 3. Sanity-check: index holds the small DB, worktree still has the big one
git cat-file -s "$BLOB"      # expect ~170 KB, NOT 273 MB
ls -lh pipeline.db           # expect 273 MB, unchanged
```

Then commit **only** these paths — never `git add -A`, never `git commit -a`:

- `pipeline.db` (the staged compact blob)
- `portfolio_state.json`
- `portfolio.py` — **only if** a `CONVICTION` multiplier changed

```bash
git commit -m "Deploy <instrument> <id> to the FIX book" \
  -- pipeline.db portfolio_state.json     # add portfolio.py only if changed
git push origin feature/ctrader-adapter
```

Zeabur auto-deploys `feature/ctrader-adapter`.

### Verify in RUNTIME logs, not the build

**A successful build is not evidence the sleeve is live.** The mounted
`/data/pipeline.db` is a persistent volume and can retain older state, so the
container can build clean and still run the previous book. Confirm the sleeve id
appears in the Zeabur **runtime** logs before reporting the deploy as done. If it
doesn't, the volume is stale — say so rather than assuming propagation lag.

`scripts/build_deploy_db.py --no-units` omits `sleeve_units`. Use it on a **fresh**
volume: those rows are the persisted broker-share truth under netting, and shipping
local units onto an empty volume makes the runtime believe it owns positions it
doesn't. On an existing volume the mounted DB keeps its own rows, so the flag is moot.

## C. Retire

```bash
./venv/bin/python -c "import pipeline_utils as p; p.retire_strategy('<id>', reason='<why>')"
```

`retire_strategy(strategy_id, reason='manual_retirement', flatten=True, force=False)`.

**Leave `flatten=True`.** Retiring stops the sleeve's `live_test` process, so any
position it still owns becomes **unmanaged and unstopped** — under netting the broker
holds no stop, because the stop is evaluated per-bar inside that loop. The function
flattens first and **raises on `MARKET_HALTED`**; let it. A still-running sleeve is
strictly safer than a stranded one.

`force=True` retires anyway and records the residual in `status_history`. Use it only
when the user explicitly accepts an unmanaged position, and tell them that's what
they're accepting.

Retiring a sleeve that is genuinely **flat** needs no close — but verify flatness
against `sleeve_units`, don't assume it.

After retiring: re-run steps A.3 (weights), the deployed-risk check, and montecarlo —
removing a sleeve **inflates** every remaining position when a cluster cap binds.

Stranded units from earlier failures: `flatten_orphans.py`.

## Post-deploy checklist

1. `run_portfolio.sh` — no sleeves silently dropped
2. deployed risk re-checked; base `RISK` unchanged at 0.005
3. `stress_book.py` — alignment and worst book-day not materially worse
4. montecarlo real-sized path — worst day still clear of −3%
5. Zeabur **runtime** logs show the sleeve id
6. `git show --stat HEAD` — exactly the allowed paths, `pipeline.db` ~170 KB
