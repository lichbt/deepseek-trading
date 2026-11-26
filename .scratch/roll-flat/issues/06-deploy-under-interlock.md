# Deploy to the pod

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
Blocked by: 04, 05

## Question

Ship it. This is a TRADING ACTION and the `sleeve-ops` deploy reference is mandatory — do
not improvise from memory.

Non-negotiables:

- `./scripts/zeabur_interlock.sh on` → confirm **0 pods** → push → `off`. A plain push
  overlaps two runners, because with `replicas=1` the default RollingUpdate starts the new
  pod before killing the old, and both would trade the same account blind to each other.
- The mounted `/data/pipeline.db` is seeded only when absent, so a push never refreshes the
  book — use `reset-db`, and NEVER `reset-volume` unless the account is flat.
- Under `RUNNER_MODE=cron` the pod boots and WAITS. A green deploy with the policy in the
  logs is not evidence it ran; `trigger-status` must show a receipt.
- Any new env var must exist in the dashboard BEFORE the build, or the running pod silently
  lacks it.

## Definition of done

Pod live on the new image, exactly one `fix_runner` for the account, state `pos_id` count
equal to `broker_positions=N`, every open position carrying a broker-side `stopLoss` with
zero standalone stop orders. Paste the log lines and the reconcile.

## Answer

**Deployed and armed. Pod `service-6a602739536b84a1337cc4bc-7f94756447-kg2h7`, ReplicaSet
`7f94756447` (2026-08-10T12:52:33Z), `ROLL_FLAT=1` verified BY VALUE.**

### The cycle, in order

```
interlock on   -> quota zz-no-trading-interlock  pods: 0/0
                  deploy 0/0, pods: none, fix_runner procs 0     <-- confirmed BEFORE pushing
push           -> 0228ca6..7a0a19e  HEAD -> feature/ctrader-adapter   (fast-forward)
build          -> new ReplicaSet 7f94756447 desired=1, held at 0 by the quota
env check      -> ROLL_FLAT present in the spec, WHILE PODS ARE STILL 0
interlock off  -> 1/1 Running, fix_runner procs 1
```

The push touched **two pod-runtime files** — `fix_runner.py` and `prop_guard.py`. The other
25 files in the 23-commit range are tests, wayfinder maps and research harnesses that the
pod never executes. No sleeve changed, so no `pipeline.db` rebuild, no `portfolio_state.json`,
and deliberately **no `reset-db`**: the book is unchanged and the volume must keep its
`fix_runner_state.json`.

### Post-deploy checklist

| # | Check | Result |
|---|---|---|
| 6 | exactly one `fix_runner` for the account | 1 pod, `fix_runner procs 1`; Mac `pgrep -f fix_runner.py` = **0**, job still `com.lich.fixtrading.plist.disabled` |
| 7 | state `pos_id` count == broker positions | **6 == 6**, and every id matches one-for-one |
| 8 | every position carries a broker-side SL | **6/6 have `sl`, 0 without**; every `stop_ref` reads `ord_status: 0` (attached) |

```
  audusd_..._i15   pos 4517172   sl=0.71183     btcusd_..._i9    pos 4496835   sl=69892.29
  eurgbp_..._i11   pos 4496838   sl=0.85064     nas100usd_..._i1 pos 4496836   sl=28009.29
  usdchf_..._i21   pos 4496831   sl=0.79591     xagusd_..._i16   pos 4496832   sl=58.825
  positions without a broker-side stopLoss: 0
```

Boot log confirms the runner did NOT trade on startup, which is what `RUNNER_MODE=cron` is
for: `RUNNER_MODE=cron — no pass on boot, no internal schedule; waiting on /data/trade_now
(poll 60s)`, `[guard] ARMED — ... sampling every 60s`.

### Proving the flag, not just its name

`env` lists names only, and `ROLL_FLAT` exists whether its value is `1`, `0` or empty — the
exact objection the `risk` command was built for ("an unarmed breaker looks exactly like an
armed one from outside"). `ROLL_FLAT`, `ROLL_FLAT_INSTRUMENTS` and `ROLL_FLAT_LEAD_MIN` are
now on that safelist, and it reports:

```
ROLL_FLAT=1        RUNNER_MODE=cron   VENUE=ctrader   PROP_GUARD_HALT=1
PROP_BROKER_CLOCK_TZ=America/New_York   BASE_RISK=0.005   BOOK_SCALE=1.1
```

`ROLL_FLAT_INSTRUMENTS` and `ROLL_FLAT_LEAD_MIN` are unset, so the compiled defaults apply:
`DE30_EUR,NAS100_USD,SPX500_USD` and 10 minutes.

### A defect found by deploying, fixed but NOT YET SHIPPED

The guard announces itself at boot; roll-flat did not. From outside, an armed policy that
acts for ten minutes a day is indistinguishable from an unset env var until the carry turns
up on a statement weeks later — the same argument that produced the `risk` command. A boot
line is now in `fix_runner._run_triggered`:

```
  [roll-flat] ARMED — closing DE30_EUR,NAS100_USD,SPX500_USD in the 10 min before the
  broker's midnight (broker clock now 2026-08-10 16:06, day 2026-08-10); reopen is the
  next ordinary pass
```

**It is committed locally and is NOT on the pod.** Shipping it needs another build, and the
interlock window costs ~10 minutes with the drawdown guard blind — not worth spending on a
log line today, with the policy already verified by value. It rides the next deploy.

## What happens next, unattended

- **~20:50 UTC today** the poll loop closes NAS100 (position 4496836) and writes `FLAT(0)`.
  DE30 and SPX500 are covered but currently flat.
- **00:15 UTC** the ordinary trigger pass re-establishes it on whatever the signal then is.
- Between those, NAS100 is flat for ~3.5h, of which ~2h is live index session. The simulator
  prices that gap at **zero** — it is 07's job to measure it, not to assume it away.

## Evidence

- Suite before the push — 1203 passed
- `interlock status / image / env / risk / state / logs` — pasted above
- Broker snapshot read directly from account 48171893
