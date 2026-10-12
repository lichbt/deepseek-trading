"""Re-run the PASSED book on CURRENT data and report which verdicts no longer hold.

WHY. A pass is a claim about the candles AND the code that produced it, and both
move underneath it. The 2026-07-31 data_fetcher dedup fix changed the candles
under ~31% of the failed pool, and nothing re-ran the passed pool at all, so
every stored pass was assumed to still hold. First full run (2026-09-24, 56
rows): 52 CLEARS_WF, 4 STILL_FAILS. This is what turns "still passing" into a
measurement.

CLOSED COPY OF THE VALIDATOR PATH. Reproduces validator.py IS + walk-forward
stages faithfully — same windows, same grid, same cost model, same gate
constants imported from validator rather than restated, so a threshold change
upstream cannot leave this stale.

DELIBERATELY STOPS BEFORE THE HOLDOUT. This sweep decides which candidates are
worth a real validation run, and the holdout is the scarce resource that
decision is spent on. Re-scoring the pool against it would be exactly the
multiple-comparisons burn that refine_codes exists to prevent. Anything flagged
here goes through the ordinary validator afterwards, once. A CLEARS_WF verdict
is NOT a pass: IS, holdout, torture, drawdown and look-ahead gates all apply.

READ-ONLY by default. --write-cause fills in validation_results.failure_cause
for rows it re-ran. --write-status is the destructive one: every wf-bucket row
whose re-run no longer clears the WF gate is demoted to FAILED_DEPLOY with the
new score as its status_history reason, so a stale pass can neither deploy nor
hide. ERROR rows are left alone — a failed measurement is not evidence against
the stored verdict.
Scheduled weekly by run_revalidate.sh (com.lich.revalidate).

Usage:  python3 scripts/revalidate_passed.py [--limit N] [--sample N]
                                     [--bucket wf|misfiled|failed_deployed] [--write-cause] [--write-status] [--out F]

--bucket wf       (default) the passed / passed_but_fragile rows.
--bucket misfiled the ~22,430 terse-prose WF failures still filed as
                  research_failed — same scoring path, failed pool.
"""

import argparse, json, sqlite3, sys, time, warnings
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/lich/deepseek-oanda-trading')

import validator as V
import pipeline_utils as pu
import refine_codes as R
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline_utils import DB_PATH as DB          # noqa: E402
from backfill_instrument import infer_instrument
from portfolio import _infer_archetype  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, default=0)
ap.add_argument('--bucket', choices=('wf', 'misfiled', 'failed_deployed'),
                default='wf',
                help="'wf' passed / passed_but_fragile rows - the default; "
                     "'failed_deployed' = stored passes an older sweep already "
                     "revoked; 'misfiled' = 22,430 terse-prose WF failures still "
                     "sitting in research_failed (see _RE_WF_GATE_TERSE)")
ap.add_argument('--sample', type=int, default=0,
                help='even stride across the ordered bucket; ids are '
                     'timestamped, so --limit would take one era only')
ap.add_argument('--write-cause', action='store_true')
ap.add_argument('--write-status', action='store_true',
                help='DESTRUCTIVE: demote stale wf-bucket passes to failed_deployed')
ap.add_argument('--out', default='revalidate_sweep.json')
a = ap.parse_args()

con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
_WF_STATUSES = (('failed_deployed',) if a.bucket == 'failed_deployed'
                else ('passed', 'passed_but_fragile'))
if a.bucket == 'misfiled':
    rows = con.execute("""
        SELECT s.id, s.code, s.param_grid, s.instrument, s.timeframe,
               s.archetype, s.instrument2, s.status,
               h.reason AS reason
        FROM strategies s
        JOIN status_history h ON h.strategy_id = s.id
        WHERE s.status = 'research_failed'
          AND (h.reason LIKE 'FAIL: WF %' OR h.reason LIKE 'WF %')
          AND h.reason LIKE '%0.0000 <%'
        GROUP BY s.id
        ORDER BY s.id
    """).fetchall()
else:
    rows = con.execute(f"""
        SELECT s.id, s.code, s.param_grid, s.instrument, s.timeframe,
               s.archetype, s.instrument2, s.status,
               (SELECT h.reason FROM status_history h
                 WHERE h.strategy_id = s.id ORDER BY h.id DESC LIMIT 1) AS reason
        FROM strategies s
        WHERE s.status IN ({','.join('?' * len(_WF_STATUSES))})
        ORDER BY s.id
    """, _WF_STATUSES).fetchall()
# holdout_failed is DELIBERATELY out of scope. Those rows cleared the WF gate at
# validation time by definition, so "clears WF today" is their status quo and
# says nothing about staleness — reporting it as a recovery would have counted
# 241 phantoms. Deciding whether they still fail needs the holdout re-scored,
# which is the exact multiple-comparisons burn this sweep refuses to spend.
if a.sample and len(rows) > a.sample:
    stride = max(1, len(rows) // a.sample)
    rows = rows[::stride][:a.sample]
if a.limit:
    rows = rows[:a.limit]

KNOWN = [r[0] for r in con.execute(
    "SELECT DISTINCT instrument FROM strategies WHERE instrument IS NOT NULL")]

print(f"re-validating {len(rows)} strategies from the {a.bucket!r} bucket", flush=True)
print(f"gates: IS>={V.MIN_IS_SCORE}  WF>={V.MIN_WF_SCORE}", flush=True)

recovered, tally, results = [], {}, []
results_err = {}   # sid -> error text, kept OUT of failure_cause
t_start = time.time()

for i, r in enumerate(rows, 1):
    t0 = time.time()
    sid, verdict, is_s, wf_s, cause = r['id'], 'ERROR', None, None, None
    try:
        inst = r['instrument'] or infer_instrument(sid, KNOWN)
        if not inst:
            raise RuntimeError('instrument unresolvable from id — skipped, not guessed')
        tf = r['timeframe'] or 'D'
        arch = _infer_archetype(r['code'], r['archetype'] or 'standard')
        dev_start, dev_end = V.get_dev_window(inst)

        full = get_candles_date_range(inst, dev_start, V.HOLDOUT_START, granularity=tf)
        if full is None or not len(full):
            raise RuntimeError('no data in dev+wf window')
        if arch != 'standard':
            full = inject_supplementary_data(full, arch, inst, r['instrument2'],
                                             dev_start, V.HOLDOUT_START, tf)

        func = V.create_strategy_function(r['code'])
        grid = json.loads(r['param_grid'])

        # The validator ADDS an ATR stop to every grid (validator.py:407-409)
        # and compute_net_strategy_returns only models the live stop when
        # 'stop_mult' is present — otherwise it silently uses the legacy no-stop
        # path. Omitting it here measured every strategy WITHOUT stops, and the
        # difference is not marginal: gbpjpy_auto_20260705_011344_i31 scores
        # 0.9638 without and 0.0000 with. Nine "recoveries" reported on
        # 2026-08-08 were entirely that artifact; all nine then failed real
        # validation. Reproduce the validator's grid, do not approximate it.
        search_grid = dict(grid)
        if 'stop_mult' not in search_grid:
            search_grid['stop_mult'] = list(pu.STOP_MULT_SWEEP)
        wf = pu.walk_forward(full, func, search_grid, n_windows=5, instrument=inst,
                             granularity=tf, apply_costs=True)
        wf_s = wf.get('combined_gt_score')
        oos = wf.get('all_oos_returns')

        zr = pu.gt_score_zero_reason(oos)
        cause = (R.classify(r['status'], r['reason'] or '', zero_reason=zr)
                 if zr is not None else None)

        if wf_s is not None and wf_s >= V.MIN_WF_SCORE:
            verdict = 'CLEARS_WF'
            recovered.append((sid, r['status'], inst + ('*' if r['instrument'] is None else ''),
                              tf, round(wf_s, 4)))
        elif zr is not None:
            verdict = f'ZERO:{cause}'
        else:
            verdict = 'STILL_FAILS'
    except Exception as e:
        verdict = 'ERROR'
        # Deliberately leave `cause` as None. failure_cause holds refine_codes
        # partition names and nothing else; writing an exception class into it
        # ("RuntimeError") pollutes the column with a value no consumer can
        # interpret, and a row that failed to re-run has NOT been classified.
        err = f'{type(e).__name__}: {e}'[:80]
        results_err[sid] = err

    tally[verdict] = tally.get(verdict, 0) + 1
    results.append({'id': sid, 'status': r['status'], 'verdict': verdict,
                    'wf': wf_s, 'cause': cause, 'instrument': locals().get('inst'),
                    'inferred': r['instrument'] is None,
                    'error': results_err.get(sid),
                    'secs': round(time.time() - t0, 1)})
    if i % 10 == 0 or verdict == 'CLEARS_WF':
        el = time.time() - t_start
        print(f"[{i}/{len(rows)}] {sid[:40]:40s} {verdict:22s} "
              f"wf={wf_s if wf_s is None else round(wf_s,4)} "
              f"({el/60:.0f}m elapsed, {el/i*(len(rows)-i)/60:.0f}m left)", flush=True)

if a.write_cause:
    n = 0
    for res in results:
        if res['cause']:
            con.execute("UPDATE validation_results SET failure_cause=? WHERE strategy_id=?",
                        (res['cause'], res['id']))
            n += 1
    con.commit()
    print(f"\nwrote failure_cause on {n} rows")

if a.write_status:
    n = 0
    for res in results:
        # Only the wf bucket holds deployable rows, and only a verdict that
        # stopped clearing the gate is evidence against the stored pass:
        # CLEARS_WF means it still reproduces, ERROR means we could not measure.
        if res['status'] not in pu.DEPLOYABLE:
            continue
        if res['verdict'] in ('CLEARS_WF', 'ERROR'):
            continue
        wf = res['wf']
        reason = (f"STALE VERDICT: revalidate_passed sweep, WF now "
                  f"{wf if wf is None else round(wf, 4)} < {V.MIN_WF_SCORE} on "
                  f"current data")
        if res['cause']:
            reason += f" [{res['cause']}]"
        pu.mark_failed_deployed(res['id'], reason)
        n += 1
    print(f"\nrevoked {n} stale passes -> {pu.FAILED_DEPLOY}")

print("\n=== verdict tally ===")
for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
    print(f"  {k:24s} {v:4d}  ({v/len(rows)*100:.0f}%)")

print(f"\n=== clears the WF gate on current data: {len(recovered)} ===")
for sid, st, inst, tf, s in sorted(recovered, key=lambda x: -x[4]):
    print(f"  {s:6.4f}  {sid[:46]:46s} {st:20s} {inst} {tf}")
print("\nNOTE: clearing WF is not passing validation — IS, holdout, torture,")
print("drawdown and look-ahead gates all still apply. Run these through the")
print("ordinary validator, once each.")

Path(a.out).write_text(
    json.dumps({'tally': tally, 'n': len(rows), 'recovered': recovered,
                'results': results}, indent=2, default=str))
print(f"\nfull results -> {a.out}")
