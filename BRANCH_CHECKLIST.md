# Branch review checklist — The5ers cTrader FIX live-trading path

Branch: `claude/friendly-taussig-1ab717`. This is a **live-money** path on a funded
prop account ($2,479.82, The5ers: 5% daily DD, 10% static DD, +8% target). Priority
of review = anything that can (a) lose more than intended, (b) breach a prop DD limit,
(c) leave an unprotected or orphaned position, (d) place a wrong/duplicate order.

Files in scope: `fix_adapter.py`, `fix_runner.py`, `run_fix_trading.sh`,
`launchd/com.lich.fixtrading.plist`. Reference (OANDA netting, unchanged behavior):
`live_test.py`, `portfolio.py`.

## A. Order execution correctness (fix_adapter.py)
- [ ] Symbol(55) is the numeric cTrader security id for every traded instrument; a missing
      map entry fails loud (skip), never sends a wrong/empty symbol.
- [ ] Buy vs sell Side(54) maps correctly to signal sign; `execute_order(sig*units)` sign
      convention is consistent end-to-end.
- [ ] Hedging close uses opposite Side + PosMaintRptID(721) of the exact position; it cannot
      accidentally net/reduce a *different* sleeve's position.
- [ ] Fill price read from AvgPx(6) with LastPx(31) fallback — realized P&L uses the fill,
      not the request price.
- [ ] `open_pos_ids()` / RequestForPositions(AN) sends only PosReqID(710); parses AP
      PositionReport (721 PosID, 704/705 long/short qty) correctly.

## B. Stop-loss integrity (the core safety claim)
- [ ] Every live `OPEN` places a broker-side protective stop (STOP 40=3, 721-linked) at
      `close - sig*stop_mult*atr`; stop side/price is correct for both long and short.
- [ ] On any close/signal-change, the broker stop is **cancelled first** (`cancel_stop`) so it
      cannot orphan into a new opposite position after the position is gone.
- [ ] Software-stop backstop triggers on the correct side (`side>0: px<=stop`, `side<0: px>=stop`)
      and also cancels the broker stop before closing.
- [ ] `stop_ref` is persisted in state and survives a process restart (state file round-trips
      the fields `cancel_stop` needs).

## C. Reconciliation & state (no double / no orphan)
- [ ] `run_once` snapshots broker positions first; a sleeve whose `pos_id` is gone at broker is
      marked FLAT (stop fired / manual close) and not re-closed.
- [ ] After a reconcile-to-flat, it does **not** re-enter until the signal actually changes
      (no same-bar re-entry into a stopped-out trade).
- [ ] Empty/first-run state + an existing broker position cannot cause a duplicate open of the
      same instrument. (Known gap: state cleared while a broker position is open → double. Is it
      guarded or only documented?)
- [ ] State file is written after stop-only passes too, so reconcile results persist.

## D. Position sizing & risk cap (fix_runner.py)
- [ ] `size_units` respects VOL_SPEC min-lot/step per instrument; rounding never rounds a
      sub-min size UP into a position larger than intended.
- [ ] Min-lot risk guard: if implied risk `units*stop_mult*atr*q2usd/equity > MAXRISK(2%)`,
      it SKIPS the open (verified: gold/copper/BTC/WTI/XPD skip at $2.5k). No off-by-one that
      lets a 2%+ trade through.
- [ ] `q2usd` conversion is correct for JPY-quoted and cross pairs; a wrong rate = wrong risk.
- [ ] RISK/MAXRISK read from env (`FIX_RISK`); aggregate "full book day one" risk is the user's
      accepted choice, but flag if any single sleeve can exceed MAXRISK.
- [ ] Equity used for sizing is the self-tracked FIX equity (start + realized + unrealized),
      and `maybe_reconcile` snaps it to the real broker balance daily so DD limits use truth.

## E. Scheduling (FIX_RUN_AT) & liveness
- [ ] With `FIX_RUN_AT` set: full trade pass on startup, full pass at HH:MM UTC daily, and
      `trade=False` stop-only passes hourly in between (entries only at the scheduled time).
- [ ] `_seconds_until` never returns 0/negative or a >24h value (no busy-loop, no missed day).
- [ ] Without `FIX_RUN_AT`: unchanged hourly-trade behavior.
- [ ] launchd `KeepAlive` + `run_fix_trading.sh` while-loop restart cannot spawn two concurrent
      runners (one FIX session per account — a second logon would conflict). Is there a lock?
- [ ] `run_fix_trading.sh` sources `.env` and refuses to start without `FIX_PASSWORD`.

## F. Session robustness (fix_adapter.py `_FixSession`)
- [ ] Reconnect logic: on disconnect it re-logs-on and re-reconciles open positions before
      trading again (won't act on a stale in-memory position map).
- [ ] Sequence-number handling: gap/reset handled; a seqnum reject can't wedge the session into
      a silent no-op that still reports "running".
- [ ] Heartbeat/TestRequest answered so the broker doesn't drop the session mid-day.

## G. Blast-radius / fail-safe
- [ ] Any per-sleeve exception (signal fetch, sizing, order send) is caught and skips that sleeve
      only — one bad sleeve can't abort the whole pass and leave the rest unmanaged.
- [ ] A failed `place_stop` after a successful `execute_order` does not leave a naked position
      silently — it's logged loudly / retried / or the position is closed.
- [ ] Dry-run (`--once` without `--live`) places NO orders and mutates no broker state.

## Output format
Review the actual code against each item. For each SECTION give: PASS / FAIL / RISK, the
specific file:line, and the concrete failure scenario if not PASS. Rank issues by blast radius
(money loss / DD breach / orphan-or-naked position first). End with exactly one line:

{"verdict":"PASS|FAIL","result":"<1-line overall>","reason":"<top issue if FAIL>"}
