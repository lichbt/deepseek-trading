#!/usr/bin/env python3
"""Gate the research loop on a token budget and a run window.

WHY: measured 2026-08-22, the loop burns ~1.2M tokens/hour. Running 24/7 is
~205M tokens/week against a plan of roughly 14.6M — about 14x over. No prompt or
model tuning closes a gap that size, so the loop has to stop itself.

Exit 0 = clear to run, exit 1 = hold. run_forever.sh calls this before each
batch and sleeps on a hold.

HONEST LIMITS, read before trusting a number:
  * This counts only what auto_research.py logged to usage.jsonl. Anything else
    billed to the same account (probes, other tools, another machine) is
    invisible here, so the reading is a FLOOR on real spend, never a ceiling.
  * usage.jsonl starts when the accounting was added; before that there is no
    history, so a rolling window reads low until it has filled.
  * Cached input is counted at face value. Providers bill it at a discount, so a
    high cache-hit run is charged less than this reports — again, a floor.
Set the cap below what the plan actually allows and leave headroom.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from llm_prices import cost_of
except Exception:                       # price table missing -> tokens only
    cost_of = None

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO / '.auto-research-logs' / 'usage.jsonl'

# Defaults live HERE, in tracked code, not only in .env.
# On 2026-08-22 a budget block appended to .env vanished within the hour —
# rewritten at 14:11:08 by something outside this repo (no .py or .sh here
# writes .env). With config only in .env, that wipe silently turns this gate
# into a no-op: the loop runs unthrottled and nothing reports it. So the cap
# and window default to real values and .env only OVERRIDES them.
# Raise these only alongside a bigger plan.
# The window is NIGHTS, deliberately: deepseek-v4-pro-0813 (the codegen head)
# bills at 50% between 22:00 and 08:00, so the same research costs half the
# credits. That discount is the reason the cap below is ~2x the raw plan.
#
# The cap counts TOKENS; the plan is denominated in CREDITS. At night the
# deepseek legs convert at half rate while the qwen critique leg does not
# (~15% of tokens in the first sample), so the blended rate is ~0.58 and a
# ~14.6M-credit plan buys ~25M tokens. 24M leaves headroom for that estimate
# being wrong. If the discount turns out not to apply, HALVE this.
# COST, not tokens. Caching does not reduce token COUNT — a cached token is
# still a token — so a token cap cannot see the 3.5x cost reduction measured on
# 2026-08-22 and would throttle at roughly a third of the real budget.
#
# Calibration, and its error bars: the dashboard showed 3.66M tokens = ~25% of
# the weekly plan. Priced at the pre-change blended rate (~$1.18/M) that is
# ~$4.32, implying a plan worth roughly $17/week. That figure is an ESTIMATE
# built on a modelled batch mix, so the cap below keeps headroom under it.
# RECALIBRATE after one full night: compare this reading against the percentage
# the console reports consumed, and scale.
DEFAULT_CAP_USD = 14.0
DEFAULT_CAP = 24_000_000        # token fallback, only for unpriced models
DEFAULT_CAP_DAYS = 7
# 9h/night. Measured 2026-08-22 at 4.25 batches/h and $0.0491/batch at the night
# rate, a $14 cap buys ~9.6 h/night — so the clock and the cap now bind at almost
# the same point and the loop runs effectively the whole night, every night.
# Runs the FULL 22:00-08:00 band as of 2026-08-22, after the codegen head swap
# took a night batch to $0.0284: nights now cost ~$8.5/week against a $14 cap,
# so the clock binds and the money does not. The old hour of margin at 07:00 was
# dropped deliberately — if the discount's timezone is off by an hour, the last
# hour bills at peak for about $0.11/night, which the headroom absorbs.
# Running into the DAY is
# the one thing to avoid — daytime batches cost $0.0862 vs $0.0491, so a
# 24/7 week would be ~$50 and 71% of it would be the 2x daytime premium.
# Widened from 3h once the cost cap replaced the token cap: at the
# measured $0.054/batch, ~$14/week buys ~260 batches, which is roughly a full
# night every night. Kept inside 22:00-08:00 with margin at both ends because
# the discount's timezone is NOT stated in the console — this window sits inside
# the band whether it is read as UTC+7 or UTC+8. The COST cap, not the clock, is
# now the binding constraint; if a week ends well under budget, widen further.
# Kept in SYNC with .env (2026-09-03: 23:00-06:00). The default exists because a
# .env wipe once silently turned this gate into a no-op — but the fail-safe here
# is the COST cap, not the clock, so the default tracks the intended window
# rather than a narrower one that would silently shrink research after a wipe.
# Both ends stay inside the 22:00-08:00 discount band.
DEFAULT_RUN_WINDOW = '23:00-06:00'

# Config lives in the repo .env, not the shell: launchd does not source ~/.zshenv
# and the failure would be silent (see the 2026-08-21 decision). Existing env
# vars still win, so an operator override on the command line holds.
sys.path.insert(0, str(REPO))
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass


def tokens_since(log_path, cutoff):
    """Total tokens logged at or after `cutoff`. Malformed lines are skipped."""
    total = 0
    counted = 0
    cost = 0.0
    unpriced = 0
    path = Path(log_path)
    if not path.is_file():
        return 0, 0, 0.0, 0
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec['ts'])
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            tot = rec.get('total_tokens')
            if tot is None:
                # An unmetered call still cost something; we just cannot see how
                # much. Count it as zero but never as "no call happened".
                tot = 0
            total += tot
            counted += 1
            if cost_of is not None and tot:
                # Price at the record's own local hour: the deepseek legs bill
                # at half between 22:00-08:00, so a flat rate would overstate a
                # night run by ~2x on those stages.
                c = cost_of(rec, ts.astimezone())
                if c is None:
                    # A model absent from the price table would otherwise be
                    # billed at ZERO and silently eat the budget. Count its
                    # tokens so the caller can refuse to trust the total.
                    unpriced += tot
                else:
                    cost += c
    return total, counted, cost, unpriced


def in_run_window(window, now):
    """`window` is "HH:MM-HH:MM" in LOCAL time. Empty/None means always open.

    A window whose end is before its start wraps midnight (22:00-04:00).
    """
    if not window:
        return True, 'no window set'
    try:
        start_s, end_s = window.split('-')
        start = datetime.strptime(start_s.strip(), '%H:%M').time()
        end = datetime.strptime(end_s.strip(), '%H:%M').time()
    except Exception:
        return True, f'unparseable window {window!r} — treating as always open'
    cur = now.time()
    if start <= end:
        ok = start <= cur < end
    else:
        ok = cur >= start or cur < end
    return ok, f'{cur.strftime("%H:%M")} vs {window}'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--log', default=str(DEFAULT_LOG))
    ap.add_argument('--cap-usd', type=float,
                    default=float(os.getenv('LLM_COST_CAP_USD') or DEFAULT_CAP_USD),
                    help='USD cap for the window (0 disables it)')
    ap.add_argument('--cap', type=float,
                    default=float(os.getenv('LLM_TOKEN_CAP') or DEFAULT_CAP),
                    help='token cap for the window (0 disables the cap)')
    ap.add_argument('--window-days', type=float,
                    default=float(os.getenv('LLM_CAP_DAYS') or DEFAULT_CAP_DAYS),
                    help='rolling budget window in days (default 7)')
    ap.add_argument('--run-window',
                    default=os.environ.get('RESEARCH_WINDOW', DEFAULT_RUN_WINDOW),
                    help='local-time window "HH:MM-HH:MM"; empty = always open')
    ap.add_argument('--status', action='store_true',
                    help='always exit 0; just print the reading')
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=args.window_days)
    used, calls, spent, unpriced = tokens_since(args.log, cutoff)

    holds = []
    open_now, why = in_run_window(args.run_window, datetime.now())
    if not open_now:
        holds.append(f'outside run window ({why})')

    if args.cap_usd > 0:
        pct = 100.0 * spent / args.cap_usd
        budget = (f'${spent:.2f}/${args.cap_usd:.2f} ({pct:.0f}%) over {args.window_days:g}d, '
                  f'{used/1e6:.2f}M tokens, {calls} calls')
        if unpriced:
            # Never let an unpriced model read as free. Fall back to the token
            # cap, which cannot under-count, and say why.
            budget += f' | WARNING {unpriced/1e6:.2f}M tokens on models missing from llm_prices.PRICES'
            if args.cap > 0 and used >= args.cap:
                holds.append(f'token cap reached (cost unreliable — unpriced models) — {budget}')
        if spent >= args.cap_usd:
            holds.append(f'cost cap reached — {budget}')
    elif args.cap > 0:
        pct = 100.0 * used / args.cap
        budget = f'{used/1e6:.2f}M/{args.cap/1e6:.2f}M ({pct:.0f}%) over {args.window_days:g}d, {calls} calls'
        if used >= args.cap:
            holds.append(f'token cap reached — {budget}')
    else:
        budget = f'{used/1e6:.2f}M over {args.window_days:g}d, {calls} calls, no cap set'

    if holds and not args.status:
        print(f'HOLD: {"; ".join(holds)}')
        return 1
    print(f'OK: {budget}' + (f' | {why}' if args.run_window else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
