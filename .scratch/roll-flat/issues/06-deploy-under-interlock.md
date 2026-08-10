# Deploy to the pod

Type: task
Status: open
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
