#!/bin/bash
# One gated command to publish the book to the Zeabur prop pod.
#
# Replaces: build deploy DB -> stage blob -> commit on the pod branch -> interlock
# on -> push -> poll image -> reset-db -> off -> trigger -> verify. Two of those
# steps fail SILENTLY by hand:
#   * git add pipeline.db stages the 485 MB RESEARCH DB (2026-08-05 incident). Here
#     the blob is only ever written by hash-object + update-index, so the working DB
#     cannot be committed.
#   * a die after interlock on leaves the pods=0 quota up: production DOWN and
#     nothing alerts. The trap below always releases the quota.
#
# The BOOK is changed in the working DB first (retire_strategy / promote / weights).
# This script only packages and ships it.
#
# Usage: scripts/deploy_book.sh --reason "why" [--no-trigger] [--dry-run] [--force]
set -euo pipefail

cd "$(dirname "$0")/.."
PODWT="$PWD/.podwt"
BRANCH=feature/ctrader-adapter
STATE=portfolio_state.json
DEPLOY_DB=/tmp/deploy_book.db
OLD_STATE=/tmp/deploy_book_old_state.json
NOTES="$PWD/.paper-trading-logs/deploy_book.log"
PY=./venv/bin/python
Z=./scripts/zeabur_interlock.sh

REASON=""; DO_TRIGGER=1; DRY=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reason) REASON="${2:-}"; shift 2;;
    --no-trigger) DO_TRIGGER=0; shift;;
    --dry-run) DRY=1; shift;;
    --force) FORCE=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$REASON" ] || { echo "usage: scripts/deploy_book.sh --reason why" >&2; exit 2; }

say() { printf "\n== %s\n" "$*"; }
run() { if [ "$DRY" = 1 ]; then printf "DRY  %s\n" "$*"; return 0; fi; printf "+    %s\n" "$*"; "$@"; }

# ---- 0. never straddle the daily pass ----------------------------------------
H=$(date -u +%H%M)
if [ "$FORCE" = 0 ] && [ "$H" -ge 0005 ] && [ "$H" -le 0035 ]; then
  echo "UTC $H is inside the 00:05-00:35 pass window; wait or pass --force" >&2
  exit 3
fi

# ---- 1. the pod worktree must be clean and on the deploy branch -----------------
[ -e "$PODWT/.git" ] || { echo "missing $PODWT: git worktree add .podwt $BRANCH" >&2; exit 1; }
DIRTY=$(git -C "$PODWT" status --porcelain)
[ -z "$DIRTY" ] || { echo "$PODWT is dirty: $DIRTY" >&2; exit 1; }
AT=$(git -C "$PODWT" rev-parse --abbrev-ref HEAD)
[ "$AT" = "$BRANCH" ] || { echo "$PODWT is on $AT, expected $BRANCH" >&2; exit 1; }
git -C "$PODWT" show HEAD:"$STATE" > "$OLD_STATE"

# ---- 2. rebuild the artifacts: weights, then the compact deploy DB --------------
say "recompute weights"
run $PY portfolio.py --write
say "build compact deploy DB"
run $PY scripts/build_deploy_db.py --out "$DEPLOY_DB" --no-units

if [ "$DRY" = 1 ]; then
  DB_BYTES=DRY; N_NEW=DRY
else
  DB_BYTES=$(stat -f%z "$DEPLOY_DB" 2>/dev/null || stat -c%s "$DEPLOY_DB")
  if [ "$DB_BYTES" -gt 5000000 ]; then echo "deploy DB is $DB_BYTES bytes - refusing" >&2; exit 1; fi
  N_NEW=$($PY -c "import json; print(json.load(open(\"$STATE\"))[\"n_strategies\"])")
fi
say "new book: $N_NEW sleeves, deploy DB $DB_BYTES bytes"

# ---- 3. commit on the pod branch: blob by hash, never git add the working DB ----
if [ "$DRY" = 1 ]; then
  say "DRY: hash-object + update-index + commit + checkout-index"
else
  BLOB=$(git -C "$PODWT" hash-object -w "$DEPLOY_DB")
  git -C "$PODWT" update-index --cacheinfo 100644,"$BLOB",pipeline.db
  cp "$STATE" "$PODWT/$STATE"
  git -C "$PODWT" add "$STATE"
  git -C "$PODWT" commit -q -m "$REASON"
  git -C "$PODWT" checkout-index -f pipeline.db
  git -C "$PODWT" show --stat --format=%h HEAD | tail -4
fi

if [ "$DRY" = 1 ]; then say "DRY: interlock on / push / poll image / reset-db / off / trigger / verify / sizing-knob check"; exit 0; fi

# ---- 4. interlock on; the trap always releases it --------------------------------
ENGAGED=0
release() {
  if [ "$ENGAGED" = 1 ]; then
    echo "releasing interlock after a failure" >&2
    "$Z" off >/dev/null 2>&1 || echo "WARN: could not release the quota - CHECK THE POD" >&2
    ENGAGED=0
  fi
}
trap release EXIT
say "interlock on"
run "$Z" on
ENGAGED=1
pods_running() { "$Z" status | sed -n "/=== pods ===/,/=== fix_runner ===/p" | grep -c Running || true; }
for i in $(seq 1 20); do
  if [ "$(pods_running)" = 0 ]; then break; fi
  sleep 5
done
LEFT=$(pods_running)
if [ "$LEFT" = 0 ]; then echo "0 pods confirmed"; else echo "still $LEFT pod line(s) - aborting" >&2; exit 1; fi

# ---- 4b. snapshot the sizing knobs (they live ONLY in the deployment) ------------
# BASE_RISK and BOOK_SCALE are set with `kubectl set env` on the k3s deployment:
# NOT in the Zeabur dashboard, NOT in .env — the pod's own log even tells you to
# "set BASE_RISK / BOOK_SCALE in .env", which is false for the pod. A rollout
# reconciles the spec back to the dashboard value, and that re-sizes EVERY
# position in the book (0.0145 -> 0.007 halves it; BOOK_SCALE -> 1 adds ~5%).
# Measured 2026-10-12: BOOK_SCALE=0.9511 was hand-set to hold magnitude after the
# hk33hkd_i26 retire, and none of the checks below would have noticed it going away.
KNOBS_BEFORE=$("$Z" risk 2>/dev/null | grep -E '^(BASE_RISK|BOOK_SCALE)=' | sort | tr '\n' ' ' || true)

# ---- 5. push, then wait for the build -------------------------------------------
say "push $BRANCH"
run git -C "$PODWT" push origin "$BRANCH"
OLD_TAG=$("$Z" image 2>/dev/null | head -1 | sed "s/.*://")
say "waiting for the image (was $OLD_TAG)"
NEW_TAG=$OLD_TAG
for i in $(seq 1 40); do
  T=$("$Z" image 2>/dev/null | head -1 | sed "s/.*://")
  if [ "$T" = "$OLD_TAG" ]; then sleep 15; else NEW_TAG=$T; break; fi
done
if [ "$NEW_TAG" = "$OLD_TAG" ]; then echo "image did not change in ~10 min - aborting" >&2; exit 1; fi
echo "new image $NEW_TAG"

# ---- 6. re-seed the volume, release, run one pass --------------------------------
say "reset-db (the volume keeps serving the OLD book until this runs)"
run "$Z" reset-db
say "interlock off"
run "$Z" off
ENGAGED=0
if [ "$DO_TRIGGER" = 1 ]; then say "trigger one pass"; run "$Z" trigger; fi

# ---- 7. verify: no removed sleeve left in pod state, count matches ---------------
say "verify"
# Wait for THIS deploy's pass, not a previous one. Two traps: the receipt
# pretty-prints with a space ("ok": true) so the old *"ok":true* match could
# never fire and the loop always burned the full 20 x 15s (~5 min every
# deploy); and a bare ok:true can be the PREVIOUS pass's receipt, which would
# return instantly and verify a pass that has not run. Compare `started`.
PREV=$("$Z" trigger-status 2>/dev/null | tr -d ' \n' | sed -n 's/.*"started":"\([^"]*\)".*/\1/p')
for i in $(seq 1 20); do
  R=$("$Z" trigger-status 2>/dev/null | tr -d ' \n')
  NEW=$(printf '%s' "$R" | sed -n 's/.*"started":"\([^"]*\)".*/\1/p')
  if [ -n "$NEW" ] && [ "$NEW" != "$PREV" ] && printf '%s' "$R" | grep -q '"ok":true'; then break; fi
  sleep 15
done
POD_STATE=$("$Z" state 2>/dev/null)
LOGS=$("$Z" logs 2000 2>/dev/null)
LOADED=$(printf "%s" "$LOGS" | sed -n "s/.*loaded \([0-9]*\) cTrader-tradeable sleeves.*/\1/p" | tail -1)
REMOVED=$($PY - "$OLD_STATE" "$STATE" <<EOF
import json, sys
old = json.load(open(sys.argv[1]))["weights"]
new = json.load(open(sys.argv[2]))["weights"]
print(" ".join(sorted(set(old) - set(new))))
EOF
)
echo "loaded=$LOADED expected=$N_NEW"
echo "removed: [$REMOVED]"
for sid in $REMOVED; do
  case "$POD_STATE" in
    *"$sid"*) echo "FAIL: removed sleeve still in pod state: $sid" >&2; exit 1;;
  esac
done
if [ "$LOADED" = "$N_NEW" ]; then echo "sleeve count matches"; else echo "WARN: loaded $LOADED vs state $N_NEW (check skips in the log)"; fi

# ---- 8. the sizing knobs must have survived the rollout --------------------------
KNOBS_AFTER=$("$Z" risk 2>/dev/null | grep -E '^(BASE_RISK|BOOK_SCALE)=' | sort | tr '\n' ' ' || true)
EFF_LINE=$(printf "%s" "$LOGS" | sed -n 's/.*\(RISK\/trade=[0-9.]*% x BOOK_SCALE [0-9.]* = EFFECTIVE [0-9.]*%\).*/\1/p' | tail -1)
echo "sizing: ${KNOBS_AFTER:-<unreadable>}"
[ -z "$EFF_LINE" ] || echo "pod log: $EFF_LINE"
SIZING_OK=1
if [ "$KNOBS_BEFORE" != "$KNOBS_AFTER" ]; then
  SIZING_OK=0
  {
    echo "FAIL: pod sizing knobs CHANGED across this deploy - the shipped book is not sized as intended:"
    echo "  before: ${KNOBS_BEFORE:-<unreadable>}"
    echo "  after:  ${KNOBS_AFTER:-<unreadable>}"
    echo "  A rollout reconciles kubectl-set env away. Reinstate, in this order:"
    echo "    ./scripts/zeabur_interlock.sh on     # then wait for 0 pods"
    echo "    ./scripts/pod_remote.py \"sudo -n k3s kubectl set env deployment/\$DEPLOY -n \$NS BASE_RISK=0.0145 BOOK_SCALE=0.9511\""
    echo "    ./scripts/zeabur_interlock.sh off     # releases the quota AND scales back to 1"
    echo "  (\$DEPLOY/\$NS are the constants at the top of scripts/zeabur_interlock.sh.)"
  } >&2
fi
mkdir -p "$(dirname "$NOTES")"
echo "$(date -u +%FT%TZ) image=$NEW_TAG commit=$(git -C "$PODWT" rev-parse --short HEAD) sleeves=$N_NEW removed=[$REMOVED] sizing_ok=$SIZING_OK reason=$REASON" >> "$NOTES"
echo "done - recorded in $NOTES"
[ "$SIZING_OK" = 1 ] || { echo "FAIL: sizing drift - the book is deployed but sized wrong (see above)" >&2; exit 1; }
