#!/bin/sh
set -eu

mkdir -p /data

if [ ! -f /data/pipeline.db ]; then
  cp /app/pipeline.db /data/pipeline.db
fi

if [ ! -f /data/fix_runner_state.json ]; then
  # A fresh volume must own NOTHING. This used to seed from the image's committed
  # copy, so a new deploy booted believing it held six cTrader PosIDs — four of
  # which no longer existed at the broker. On this venue a close aimed at a dead
  # PosID is not rejected, it OPENS the opposite position (no REDUCE_ONLY over
  # FIX), so inherited state does not fail safe. Start empty and let the first
  # reconcile learn what is actually open.
  echo '{}' > /data/fix_runner_state.json
fi

ln -sf /data/pipeline.db /app/pipeline.db
ln -sf /data/fix_runner_state.json /app/fix_runner_state.json

python -c 'from pipeline_utils import init_db; init_db()'
exec python -u fix_runner.py --live
