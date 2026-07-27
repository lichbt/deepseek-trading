#!/usr/bin/env bash
# Zeabur trading interlock — keep the cTrader fix_runner from ever booting there.
#
# Production is the Mac. Exactly one fix_runner may run per broker account, so
# the Zeabur deployment must stay down. Scaling it to 0 is not enough on its
# own: a git push makes Zeabur's control plane re-apply the Deployment manifest
# over the k3s API (:6443), which can reset spec.replicas. A ResourceQuota is a
# separate object the redeploy does not touch, so pods=0 holds regardless.
#
#   ./scripts/zeabur_interlock.sh status   # show quota + deployment + pods
#   ./scripts/zeabur_interlock.sh on       # scale to 0 AND add the pods=0 quota
#   ./scripts/zeabur_interlock.sh off      # remove the quota (leaves replicas 0)
#
# Credentials come from the '#zeabur SSH' block in .env (USERNAME/PASSWORD/IP);
# nothing is stored here. Requires `expect`.
set -euo pipefail

NS=environment-6a602193b0b7a4abeb4e6dca
DEPLOY=service-6a602739536b84a1337cc4bc
QUOTA=zz-no-trading-interlock

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env found" >&2; exit 1; }

RUSER=$(grep -E '^USERNAME=' .env | tail -1 | cut -d= -f2-)
RPW=$(grep -E '^PASSWORD=' .env | tail -1 | cut -d= -f2-)
RIP=$(grep -E '^IP=' .env | tail -1 | cut -d= -f2-)

# Prefer an env-supplied password so the secret need not live in .env at all:
#   ZEABUR_PASSWORD='...' ./scripts/zeabur_interlock.sh on
RPW="${ZEABUR_PASSWORD:-$RPW}"

[ -n "$RUSER" ] && [ -n "$RIP" ] || { echo "zeabur SSH USERNAME/IP missing from .env" >&2; exit 1; }
[ -n "$RPW" ] || { echo "no SSH password: set ZEABUR_PASSWORD in the environment, or PASSWORD= in .env" >&2; exit 1; }
export RUSER RPW RIP

remote() {
  RCMD="$1" expect -c '
    set timeout 90
    log_user 0
    spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR $env(RUSER)@$env(RIP) $env(RCMD)
    expect {
      -re "(?i)password:" { send "$env(RPW)\r"; exp_continue }
      -re "(?i)continue connecting" { send "yes\r"; exp_continue }
      eof
    }
    log_user 1
    puts $expect_out(buffer)'
}

K="sudo -n k3s kubectl"

case "${1:-status}" in
  status)
    remote "echo '=== quota ==='; $K get quota -n $NS;
            echo '=== deploy ==='; $K get deploy $DEPLOY -n $NS;
            echo '=== pods ==='; $K get pods -n $NS;
            echo '=== fix_runner procs ==='; ps aux | grep -c '[f]ix_runner.py'"
    ;;
  on)
    remote "$K scale deploy $DEPLOY -n $NS --replicas=0;
            $K create quota $QUOTA --hard=pods=0 -n $NS 2>&1 | tail -1;
            $K get quota -n $NS"
    ;;
  off)
    remote "$K delete quota $QUOTA -n $NS; $K get quota -n $NS"
    ;;
  volume)
    # Show what the persistent volume currently holds. The entrypoint seeds
    # /data/pipeline.db only when ABSENT and writes '{}' to fix_runner_state.json
    # only when ABSENT, so a stale volume silently keeps an old book and old
    # pos_ids no matter how many times you redeploy.
    remote "sudo find /var/lib/rancher/k3s/storage -maxdepth 3 \
              \( -name 'pipeline.db' -o -name 'fix_runner_state.json' \) \
              -printf '%p  %s bytes  %TY-%Tm-%Td %TH:%TM\n' 2>/dev/null || true"
    ;;
  reset-volume)
    # Delete both so the next pod seeds the shipped book and owns NOTHING.
    remote "sudo find /var/lib/rancher/k3s/storage -maxdepth 3 \
              \( -name 'pipeline.db' -o -name 'fix_runner_state.json' \) \
              -print -delete 2>/dev/null || true;
            echo '--- remaining ---';
            sudo find /var/lib/rancher/k3s/storage -maxdepth 3 \
              \( -name 'pipeline.db' -o -name 'fix_runner_state.json' \) 2>/dev/null || true"
    ;;
  image)
    # Which image would a pod actually run? A push triggers a Zeabur build; until it
    # finishes the Deployment still points at the PREVIOUS image, whose fix_runner
    # predates the VENUE flag and would boot into the FIX path with the old book.
    remote "$K get deploy $DEPLOY -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}{\"\n\"}';
            echo '--- replicasets (newest first) ---';
            $K get rs -n $NS --sort-by=.metadata.creationTimestamp \
              -o custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,CREATED:.metadata.creationTimestamp | tail -4"
    ;;
  logs)
    remote "$K logs -n $NS deploy/$DEPLOY --tail=\${2:-60}"
    ;;
  nudge)
    # After the quota is removed the ReplicaSet may sit in exponential backoff from
    # the pod-creation denials and take minutes to retry. A rollout restart forces a
    # fresh ReplicaSet and an immediate pod. This STARTS TRADING.
    remote "$K rollout restart deploy $DEPLOY -n $NS;
            sleep 10; $K get deploy $DEPLOY -n $NS; $K get pods -n $NS"
    ;;
  up)
    # Release the interlock AND scale to 1. This STARTS TRADING — the runner does a
    # full pass on boot. Never run it while a fix_runner is alive on the Mac.
    remote "$K delete quota $QUOTA -n $NS 2>&1 | tail -1;
            $K scale deploy $DEPLOY -n $NS --replicas=1;
            sleep 5; $K get deploy $DEPLOY -n $NS; $K get pods -n $NS"
    ;;
  *)
    echo "usage: $0 {status|on|off|volume|reset-volume|up}" >&2; exit 2 ;;
esac
