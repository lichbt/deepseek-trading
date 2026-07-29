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
  state)
    # Read-only: dump what the runner believes it owns. This is the ONLY record of a
    # past pass once the pod that ran it has been replaced and its logs are gone —
    # a sleeve with a signal but pos_id null means an open was attempted and failed,
    # while an absent sleeve means it was never evaluated.
    remote "sudo find /var/lib/rancher/k3s/storage -maxdepth 3 -name 'fix_runner_state.json' \
              -exec cat {} \; 2>/dev/null || true"
    ;;
  env)
    # NAMES ONLY — never values. The pod env holds CTRADER_TOKENS, FIX_PASSWORD and
    # broker creds; dumping it would spill them into logs and transcripts. This
    # answers "is FRED_API_KEY / FIX_RISK set?" without disclosing any secret.
    remote "$K get deploy $DEPLOY -n $NS \
              -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{\"\n\"}{end}' | sort"
    ;;
  cache)
    # Read-only: what the runner's OANDA candle cache actually holds. This is the
    # DIRECT evidence for the one-session lag. get_candles_date_range keys the cache
    # on (instrument, granularity, start, end) date strings that are constant for a
    # whole UTC day, and OANDA_CACHE_TTL_HOURS defaults to 24 — so a frame captured
    # at 00:05, before the newest daily bar closed, is still served at the 21:05
    # trade pass. An age in HOURS next to a last-bar date a day behind is the bug.
    remote "$K exec -n $NS deploy/$DEPLOY -- python -c 'import glob,os,time;import pandas as pd;
fs=sorted(glob.glob(\"/app/.cache/oanda/*.parquet\"),key=os.path.getmtime)[-8:];
print(\"%-14s %9s  %s\"%(\"cache file\",\"age\",\"last bar in frame\"));
[print(\"%-14s %6.1f h  %s\"%(os.path.basename(f)[:12],(time.time()-os.path.getmtime(f))/3600,pd.read_parquet(f)[\"date\"].max())) for f in fs]'"
    ;;
  reset-db)
    # THE ONE TO USE WHEN DEPLOYING/RETIRING A SLEEVE. Deletes ONLY pipeline.db so the
    # next pod seeds the newly shipped book, and LEAVES fix_runner_state.json alone.
    # Wiping state while positions are open makes the pod forget it owns them, so it
    # re-enters every sleeve and duplicates the book — the 2026-07-27 failure.
    remote "sudo find /var/lib/rancher/k3s/storage -maxdepth 3 -name 'pipeline.db' \
              -print -delete 2>/dev/null || true;
            echo '--- state left in place (correct) ---';
            sudo find /var/lib/rancher/k3s/storage -maxdepth 3 -name 'fix_runner_state.json' \
              -printf '%p  %s bytes\n' 2>/dev/null || true"
    ;;
  trigger)
    # Ask the runner to do ONE full trading pass. This is a TRADING ACTION.
    #
    # Under RUNNER_MODE=cron the pod never starts a pass by itself, so this file is the
    # only thing that makes it trade. Writing a file rather than `kubectl exec`-ing a
    # second fix_runner is deliberate: state is loaded once at startup and written back,
    # so two processes would clobber each other's pos_ids. The resident stays the only
    # writer, and the file waits on disk if the pod happens to be restarting.
    remote "D=\$(sudo find /var/lib/rancher/k3s/storage -maxdepth 2 -type d -name 'pvc-*data-service*' | head -1);
            [ -n \"\$D\" ] || { echo 'no data PVC found' >&2; exit 1; };
            sudo touch \"\$D/trade_now\";
            sudo ls -l \"\$D/trade_now\""
    ;;
  trigger-status)
    # Read-only. Answers the two questions that matter the morning after:
    #   * trade_now still PRESENT  -> nobody consumed it; the runner is dead or wedged.
    #   * last_pass.json age       -> when a pass last completed, and whether it succeeded.
    # The receipt exists because pod logs retain only ~3h, so by morning the log is gone
    # and "never ran" is otherwise indistinguishable from "ran and failed".
    remote "D=\$(sudo find /var/lib/rancher/k3s/storage -maxdepth 2 -type d -name 'pvc-*data-service*' | head -1);
            echo '--- pending trigger (present = NOT consumed) ---';
            sudo ls -l \"\$D/trade_now\" 2>/dev/null || echo 'none pending (correct between passes)';
            echo '--- last completed pass ---';
            sudo cat \"\$D/last_pass.json\" 2>/dev/null || echo 'no receipt yet'"
    ;;
  cron-tz-check)
    # Does THIS cron honour CRON_TZ? If not, the UTC pin silently does nothing and the
    # trigger fires 8h early on a +08 host. Debian/Ubuntu vixie-cron documents it; verify
    # rather than trust, because being wrong here means trading an unclosed bar.
    remote "echo -n 'CRON_TZ in the cron binary: ';
            sudo strings /usr/sbin/cron 2>/dev/null | grep -ic 'cron_tz' || echo 0;
            echo '--- cron package ---'; dpkg -l cron 2>/dev/null | tail -1"
    ;;
  cron-show)
    # Read-only: host clock, and whether the trigger cron is installed. The clock matters
    # because the trade time is 00:15 UTC and this host does NOT run on UTC — the guard
    # inside trade_trigger.sh therefore tests the UTC hour rather than trusting local time.
    remote "date -u '+host UTC   : %Y-%m-%d %H:%M'; date '+host LOCAL : %Y-%m-%d %H:%M %Z';
            echo '--- root crontab ---';
            sudo crontab -l 2>/dev/null || echo '(none installed)'"
    ;;
  cron-install)
    # Install the daily trigger. Until this exists, a RUNNER_MODE=cron pod trades NEVER.
    #
    # TIMEZONE: this cron is 3.0pl1-184ubuntu2 with NO CRON_TZ support (verified with
    # `cron-tz-check`), and the host runs +08 — so a UTC trade time cannot be written as
    # a schedule. Hardcoding a local hour would work today and break silently if the host
    # TZ ever moved. Instead cron fires HOURLY at :15 and the guard inside the script
    # acts only in the 00:00 UTC hour: correct regardless of host timezone or DST.
    #
    # WHY 00:15 UTC AND NOT 21:05: the broker publishes its trading schedule in
    # Europe/Bucharest, NOT UTC. In summer (UTC+3) indices/metals/oil run 01:05-23:50
    # Bucharest = 22:05-20:50 UTC, and FX runs 00:05-23:55 = 21:05-20:55 UTC. So the old
    # 21:05 UTC pass sat 15 minutes INSIDE the index close — every index order was
    # rejected before the broker even persisted it (observed 2026-07-28: nas100usd i9
    # "ENTRY FAILED", zero orders in the broker's 21:00-22:00 history) — and every FX
    # order raced the session reopening at exactly 21:05. Winter (UTC+2) shifts the index
    # window to 23:05-21:50 UTC, so the only slot open in BOTH regimes is roughly
    # 23:15-20:50 UTC. 00:15 UTC sits inside that for every instrument year-round, at the
    # cost of acting ~3h after the 21:00 UTC daily bar close instead of ~5 minutes.
    #
    # The command is a SCRIPT, not an inline crontab line, so no '%' needs escaping
    # through bash -> expect -> ssh -> crontab. Shipped base64 for the same reason.
    TRIG_SH='#!/bin/sh
# Fire the daily trading trigger in the 00:00 UTC hour. Installed by
# scripts/zeabur_interlock.sh cron-install — edit there, not here.
[ "$(date -u +%H)" = "00" ] || exit 0
D=$(find /var/lib/rancher/k3s/storage -maxdepth 2 -type d -name "pvc-*data-service*" | head -1)
[ -n "$D" ] || { echo "trade_trigger: no data PVC found" >&2; exit 1; }
touch "$D/trade_now"
'
    B64=$(printf '%s' "$TRIG_SH" | base64 | tr -d '\n')
    remote "echo '$B64' | base64 -d | sudo tee /usr/local/bin/trade_trigger.sh >/dev/null;
            sudo chmod 755 /usr/local/bin/trade_trigger.sh;
            (sudo crontab -l 2>/dev/null | grep -v 'trade_trigger\|trade_now' || true;
             echo '15 * * * * /usr/local/bin/trade_trigger.sh') | sudo crontab -;
            echo '--- script ---'; sudo cat /usr/local/bin/trade_trigger.sh;
            echo '--- crontab ---'; sudo crontab -l"
    ;;
  reset-volume)
    # ONLY safe when the account is FLAT. Wipes state too — see reset-db above.
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
    # $2 must expand LOCALLY: escaped, it reaches the remote shell as a literal
    # ${2:-60} with no positional args set, so every call silently tailed 60.
    remote "$K logs -n $NS deploy/$DEPLOY --tail=${2:-60}"
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
    echo "usage: $0 {status|on|off|volume|state|env|cache|trigger|trigger-status|cron-show|cron-install|cron-tz-check|reset-db|reset-volume|image|logs|nudge|up}" >&2; exit 2 ;;
esac
