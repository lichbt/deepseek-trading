#!/bin/sh
set -eu

mkdir -p /data

if [ ! -f /data/pipeline.db ]; then
  cp /app/pipeline.db /data/pipeline.db
fi

if [ ! -f /data/fix_runner_state.json ]; then
  cp /app/fix_runner_state.json /data/fix_runner_state.json
fi

ln -sf /data/pipeline.db /app/pipeline.db
ln -sf /data/fix_runner_state.json /app/fix_runner_state.json

python -c 'from pipeline_utils import init_db; init_db()'
exec python -u fix_runner.py --live
