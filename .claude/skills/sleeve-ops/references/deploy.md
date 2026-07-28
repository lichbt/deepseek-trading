# deploy — activate, publish, retire

**Requires an explicit "yes" naming the sleeve.** Never infer approval from a
positive evaluation.

## A. Activate on the paper book

```bash
source ~/.zshrc

# 1. DB only: status -> paper_trading, create the live_status row
./venv/bin/python -c "import pipeline_utils as p; p.start_live_trading('<id>')"

# 2. Restart the OANDA paper book — it freezes its sleeve list at process start
launchctl kickstart -k gui/$(id -u)/com.lich.papertrading
# The prop book is NO LONGER on the Mac: com.lich.fixtrading is disabled
# (~/Library/LaunchAgents/com.lich.fixtrading.plist.disabled) since the
# 2026-07-27 cutover. It restarts as a Zeabur pod — see section B.

# 3. Recompute inverse-vol weights -> portfolio_state.json
bash run_portfolio.sh 2>&1 | tail -40
```

**The restart is mandatory on BOTH books, and not optional for a retirement either.**
`run_paper_trading.sh` queries `status='paper_trading'` once before spawning children,
and `fix_runner.py` calls `load_sleeves()` *outside* its `while` loop — so the prop
book's equivalent of a restart is rolling the Zeabur pod (section B). Until each is
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
./scripts/zeabur_interlock.sh logs | grep 'cTrader-tradeable' | tail -1
```

Rolling the pod runs a **full** trading pass immediately (`first=True`), not
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

## B. Publish to Zeabur (cTrader runtime)

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

### Zeabur IS production for the prop book (since the 2026-07-27 cutover)

`service-6a602739536b84a1337cc4bc` (ns `environment-6a602193b0b7a4abeb4e6dca`) runs the
prop book with `VENUE=ctrader` — execution over the cTrader Open API, not FIX. The Mac
runs the OANDA paper book and research only; `com.lich.fixtrading` is disabled at
`~/Library/LaunchAgents/com.lich.fixtrading.plist.disabled` (renamed, not deleted, so it
can be restored). Rename it back ONLY after scaling Zeabur to 0.

**Always drive the deployment through `./scripts/zeabur_interlock.sh`** —
`status | on | off | volume | reset-db | reset-volume | image | logs | nudge | up`.

**ONE `fix_runner` PER ACCOUNT. This is an invariant, not a preference.** Both hosts
use the same `FIX_LOGIN`, so both trade the *same* The5ers account with separate state
files and no visibility of each other. On 2026-07-27 both were live: Zeabur closed
EURUSD 4313908 and HK33 4313909 while the Mac still owned them, and both claimed
DAX40 4313912 and EURGBP 4313914. Two runners double the effective size on a book whose
worst historical day is −2.79% against a 3% daily wall. Before scaling Zeabur up, stop
`com.lich.fixtrading` on the Mac — and vice versa.

**A plain push overlaps two runners.** With `replicas=1` the default RollingUpdate
starts the new pod BEFORE terminating the old one, so both trade the account for the
overlap. Always: `interlock on` -> confirm **0 pods** -> push -> `interlock off`.
Verified repeatedly on 2026-07-27: the push itself resets `spec.replicas` to 1, and only
the `pods=0` ResourceQuota holds it down.

**`git push` to this branch is a TRADING ACTION while Zeabur is up.** Zeabur
auto-deploys the branch; each deploy creates a new pod; `fix_runner` runs a full
trading pass on startup. Four pushes on 2026-07-27 were four live trading passes.
If the deployment is ever scaled back up, treat every push as an order-placing event
and time it accordingly.

### Dashboard env vars apply only on a REAL build

The pod env is a hand-maintained list in the Zeabur dashboard, and Zeabur injects it into
the Deployment manifest only when its control plane applies the manifest — i.e. on a
deploy. Two consequences, both verified 2026-07-28:

- **`nudge` does not pick up a new variable.** A rollout restart reuses the existing spec.
- **An empty commit does not either.** Zeabur skips a push with no tree change: two
  `--allow-empty` pushes produced no new ReplicaSet (newest stayed `5c47f6c998`) and no
  new image tag. To force a build, change a file.

So the order for anything env-gated is: **add the variable first, then push a real change.**
Adding it after the build means the running pod does not have it, silently. Check with
`./scripts/zeabur_interlock.sh env` (names only) BEFORE releasing the interlock — the
build applies the manifest while the quota still holds pods at 0, so there is a window
to verify before anything trades.

### RUNNER_MODE=cron — trigger-driven scheduling

`RUNNER_MODE=cron` removes both ways `fix_runner` starts a pass by itself (the `first=True`
boot pass and the `--at` schedule), so **a redeploy stops being a trading action**. The
runner then waits on `/data/trade_now`; host cron creates it in the 21:00 UTC hour.

```bash
./scripts/zeabur_interlock.sh cron-install    # install the daily trigger (root crontab)
./scripts/zeabur_interlock.sh cron-show       # host clock + installed crontab
./scripts/zeabur_interlock.sh trigger         # ask for ONE pass — a TRADING ACTION
./scripts/zeabur_interlock.sh trigger-status  # unconsumed trigger? last pass receipt?
```

Two things that make this safe rather than merely simpler:

- **One process stays the only state writer.** State is loaded once at startup and written
  back, so `kubectl exec`-ing a second `fix_runner` would clobber the resident's view and
  lose a `pos_id`. Cron writes a *file*; it never runs Python.
- **The trigger is consumed before the pass runs**, so a pass that dies halfway cannot
  re-fire and re-enter the book. A finished pass writes `/data/last_pass.json` — pod logs
  retain only ~3h, so without that receipt "never ran" and "ran and failed" look identical.

An **unconsumed `trade_now`** is the alarm: it means the runner is dead or wedged. That is
the failure mode to watch, because with no internal schedule a dead runner is silent.

The host cron is deliberately **hourly with a UTC guard inside the script**, not a `21:05`
schedule: this cron (`3.0pl1-184ubuntu2`) has no `CRON_TZ` support and the host runs +08,
so a bare `5 21 * * *` would fire eight hours early into a bar that has not closed.

Rollback is unsetting `RUNNER_MODE` and rolling the pod — never a code revert.

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
./scripts/zeabur_interlock.sh reset-db      # deploying/retiring a sleeve — USE THIS
./scripts/zeabur_interlock.sh reset-volume  # ONLY when the account is FLAT
```

**`reset-db` vs `reset-volume` is the difference between a clean deploy and duplicating
the book.** `reset-db` deletes only `pipeline.db`, leaving `fix_runner_state.json` in
place so the pod still knows which positions it owns. `reset-volume` deletes both — it
was correct for the cutover, when the account had been flattened first, and is WRONG
during a normal sleeve deploy: a pod that forgets its positions re-enters every sleeve
and doubles the book, which is the 2026-07-27 failure.

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

After retiring, **restart the paper book (A.2) and roll the Zeabur pod (B)** or the
sleeve keeps trading and re-enters on its next flip. The prop side then cleans itself
up: once the sleeve leaves the book, `sweep_orphans` cancels its stop and closes the
cTrader position on the next pass — now a `ProtoOACancelOrderReq`/`ProtoOAClosePositionReq`
pair rather than FIX orders. The OANDA side was already flattened by `retire_strategy`.

Then re-run A.3 (weights), the deployed-risk check, and montecarlo — removing a sleeve
**inflates** every remaining position when a cluster cap binds.

Stranded units from earlier failures: `flatten_orphans.py` (OANDA only — it flattens
via the OANDA REST endpoint and reads `sleeve_units`; the FIX book has its own
in-process `sweep_orphans`).

## D. Venue / host migration

A sleeve deploy changes the book. A **migration** changes where orders go or which host
sends them, and it has a different failure mode: two runners on one account, or a book
whose positions nobody owns. This is the sequence that worked on 2026-07-27 moving the
prop book from FIX-on-the-Mac to cTrader-on-Zeabur. Follow it in order; each step exists
because skipping it broke something.

1. **Prove the new venue end to end BEFORE touching the book.** Auth, reconcile, and a
   full order round trip at minimum size on a symbol no sleeve holds — open with the
   stop attached, read it back, close by id, confirm the existing book is untouched.
   Isolated order tests are safe alongside a live runner: `find_orphans` iterates the
   runner's own state file, not the broker book, so a position it never opened is
   invisible to the sweep. Unit tests cannot substitute — they mock the adapter, which
   is exactly where wire-format bugs live.

2. **Flatten first.** Cancel each resting stop, verify the cancel, THEN close by id —
   and abort that sleeve if the cancel is unconfirmed, because a stop outliving its
   position fires as a naked entry. A flat account means the new host's empty state is
   simply correct, instead of a transfer of live broker state that has to be trusted.

3. **Stop the old host and make it stay stopped.** `launchctl unload`, not `stop` — a
   stopped job respawns. Then rename the plist to `.plist.disabled`, or a reboot brings
   it back and you have two runners. Confirm `pgrep -f fix_runner.py` returns 0.

4. **Push behind the interlock.** `./scripts/zeabur_interlock.sh on`, confirm **0 pods**,
   then push. The image builds but cannot trade. Confirm the build actually landed with
   `image` — until it finishes the Deployment still points at the PREVIOUS image, which
   may predate the venue flag and would boot the old path with the old book.

5. **Wipe the volume — both files, because the account is flat.**
   `./scripts/zeabur_interlock.sh reset-volume`. This is the ONE case where wiping
   state is right; during a sleeve deploy use `reset-db`.

6. **Release**, then verify from the BROKER, not the log: expected position count,
   every position carrying a `stopLoss`, and zero standalone stop orders.

7. **Restart once more and re-verify.** The second boot is the real test — it proves
   state persisted and the pod recognises its own positions instead of re-entering.
   `broker_positions=N` with no OPEN lines is the pass condition; if it re-opens, re-arm
   the interlock immediately.

**Rollback** is unsetting `VENUE` (default `fix`) and rolling the pod — never a code
revert. Keep the old venue's adapter importable for exactly this reason.

## Post-deploy checklist

1. `run_portfolio.sh` — no sleeves silently dropped
2. deployed risk re-checked; base `RISK` unchanged at 0.005
3. `stress_book.py` — alignment and worst book-day not materially worse
4. montecarlo real-sized path — worst day still clear of −3%
5. **paper book restarted** and verified against the book (A.2); **Zeabur pod rolled**
   via `interlock on -> push -> reset-db -> off`, and `logs` shows the new sleeve count
6. **exactly one `fix_runner` is live** for the account — `./scripts/zeabur_interlock.sh
   status` shows 1 pod and `fix_runner procs 1`, AND `pgrep -f fix_runner.py` on the Mac
   returns **0**. The Mac's launchd job must stay `.plist.disabled`.
7. **state matches the broker**: `pos_id` count in `fix_runner_state.json` equals
   `broker_positions=N` in the log, and every entry maps to a real position
8. **every open position carries a broker-side SL** — reconcile shows `stopLoss` set and
   **zero standalone stop orders**. Over cTrader the stop is attached at entry
   (`stop@broker OK` in the log); a position with `sl=None` means the attach failed and
   the sleeve is software-stopped only.

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
