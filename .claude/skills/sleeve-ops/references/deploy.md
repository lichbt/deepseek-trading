# deploy — activate, publish, retire

**Requires an explicit "yes" naming the sleeve.** Never infer approval from a
positive evaluation.

## A. Activate on the paper book

```bash
source ~/.zshrc

# 1. DB only: status -> paper_trading, create the live_status row
./venv/bin/python -c "import pipeline_utils as p; p.start_live_trading('<id>')"

# 2. Restart BOTH traders — each freezes its sleeve list at process start
launchctl kickstart -k gui/$(id -u)/com.lich.papertrading
launchctl kickstart -k gui/$(id -u)/com.lich.fixtrading

# 3. Recompute inverse-vol weights -> portfolio_state.json
bash run_portfolio.sh 2>&1 | tail -40
```

**Both restarts are mandatory, and neither is optional for a retirement either.**
`run_paper_trading.sh` queries `status='paper_trading'` once before spawning children,
and `fix_runner.py` calls `load_sleeves()` *outside* its `while` loop. Until each is
restarted a new sleeve never trades and a retired one keeps trading — and a retired one
will re-enter on its next signal flip, silently undoing whatever flatten you just did.
Killing a single `live_test` child does nothing: `spawn_trader` restarts it after 30s.

Verify both took effect:

```bash
# OANDA: running loops must equal the active book
ps -eo args | grep '[l]ive_test.py' | awk '{print $NF}' | sort -u | wc -l
sqlite3 pipeline.db "select count(*) from strategies where status='paper_trading';"

# FIX: sleeves=N in the log; N is LOWER by design — load_sleeves drops instruments
# cTrader doesn't carry (WHEAT_USD, and SOYBN_USD before it was retired)
grep -E 'loaded .* cTrader-tradeable' .fix-logs/fix.log | tail -1
```

Restarting `fix_runner` runs a **full** trading pass immediately (`first=True`), not
just the hourly stop backstop — entries and exits fire off-schedule. That is expected;
it is also why you never restart it casually while investigating.

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

### Zeabur is STOPPED — the Mac is production (since 2026-07-27)

`service-6a602739536b84a1337cc4bc` (ns `environment-6a602193b0b7a4abeb4e6dca`) is
scaled to **0 replicas**. Committing and pushing is still correct — it keeps the repo
truthful and seeds any future volume — but nothing consumes it today. The Mac's
`com.lich.fixtrading` trades the prop account.

**ONE `fix_runner` PER ACCOUNT. This is an invariant, not a preference.** Both hosts
use the same `FIX_LOGIN`, so both trade the *same* The5ers account with separate state
files and no visibility of each other. On 2026-07-27 both were live: Zeabur closed
EURUSD 4313908 and HK33 4313909 while the Mac still owned them, and both claimed
DAX40 4313912 and EURGBP 4313914. Two runners double the effective size on a book whose
worst historical day is −2.79% against a 3% daily wall. Before scaling Zeabur up, stop
`com.lich.fixtrading` on the Mac — and vice versa.

**`git push` to this branch is a TRADING ACTION while Zeabur is up.** Zeabur
auto-deploys the branch; each deploy creates a new pod; `fix_runner` runs a full
trading pass on startup. Four pushes on 2026-07-27 were four live trading passes.
If the deployment is ever scaled back up, treat every push as an order-placing event
and time it accordingly.

### The volume never updates from the image

```sh
if [ ! -f /data/pipeline.db ]; then cp /app/pipeline.db /data/pipeline.db; fi
ln -sf /data/pipeline.db /app/pipeline.db
```

The copy happens **only when the file is absent**, then the image's copy is symlinked
over. So a redeploy can never refresh the book — the volume kept a 30-sleeve DB from
07-25 through every push. A green build is not evidence; confirm the sleeve id in the
**runtime** logs.

To actually refresh it, delete the volume file (the entrypoint then seeds from the
image, guaranteeing it matches the deployed commit) — with the deployment at 0:

```bash
V=/var/lib/rancher/k3s/storage/pvc-190f65e0-d88f-4396-969d-2d3b0cd6ebc0_environment-6a602193b0b7a4abeb4e6dca_data-service-6a602739536b84a1337cc4bc
ssh ubuntu@43.172.83.25 "sudo rm $V/pipeline.db $V/fix_runner_state.json"
```

Delete the state file alongside it, or the fresh book inherits PosIDs from the old one.

`fix_runner_state.json` is untracked as of `ca3179c` and the entrypoint writes `{}` —
a fresh volume owns nothing and learns positions from the broker's first reconcile.
It must never be committed again: a close aimed at a dead PosID is **not rejected over
FIX**, it opens the opposite position, so inherited state places real orders.

`scripts/build_deploy_db.py --no-units` omits `sleeve_units`, which is the right
default for the deployment DB: the image only ever seeds an empty volume, and shipping
local OANDA units onto one makes the runtime believe it owns positions it doesn't.

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

A `MARKET_HALTED` abort leaves **no record of intent** — `retire_strategy` raises
before touching the DB, so there is no pending flag and no `status_history` row. On
2026-07-25 three retirements aborted this way and were still trading two days later.
If it aborts, retry when the session reopens; don't assume it will happen on its own.

After retiring, **restart both traders** (see A.2) or the sleeve keeps trading and
re-enters on its next flip. The FIX side then cleans itself up: once the sleeve leaves
the book, `sweep_orphans` cancels its stop and closes the cTrader position on the next
pass. The OANDA side was already flattened by `retire_strategy`.

Then re-run A.3 (weights), the deployed-risk check, and montecarlo — removing a sleeve
**inflates** every remaining position when a cluster cap binds.

Stranded units from earlier failures: `flatten_orphans.py` (OANDA only — it flattens
via the OANDA REST endpoint and reads `sleeve_units`; the FIX book has its own
in-process `sweep_orphans`).

## Post-deploy checklist

1. `run_portfolio.sh` — no sleeves silently dropped
2. deployed risk re-checked; base `RISK` unchanged at 0.005
3. `stress_book.py` — alignment and worst book-day not materially worse
4. montecarlo real-sized path — worst day still clear of −3%
5. **both traders restarted**, and each verified against the book (A.2)
6. **exactly one `fix_runner` is live** for the account — `ps` on the Mac *and*
   `kubectl get deploy -n environment-…` on the Zeabur host
7. **FIX state matches the broker**: `pos_id` count in `fix_runner_state.json` equals
   `broker_positions=N` in the log, and every entry maps to a real position

   ```bash
   ./venv/bin/python -c "import json;s=json.load(open('fix_runner_state.json'));\
   print(sum(1 for v in s.values() if isinstance(v,dict) and v.get('pos_id')))"
   grep 'broker_positions' .fix-logs/fix.log | tail -1
   ```

   A mismatch means a position nobody owns. **Never repair state from a count** —
   read the actual PosIDs from the cTrader UI or the broker snapshot. A state entry
   written on a guess produces a real order: on 2026-07-27 restoring one from an
   inferred count turned a closed 0.05 AUS200 long into a live 0.10 short.
8. no naked pending stops — every resting stop order maps to an open position
9. `git show --stat HEAD` — exactly the allowed paths, `pipeline.db` ~170 KB
