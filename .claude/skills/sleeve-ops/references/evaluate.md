# evaluate — is this sleeve worth real risk?

The automated gates (WF / holdout / torture) pass strategies that are actually
directional beta or rest on code bugs. This is the last filter before real money.
**Deploy only on an explicit "yes".**

## 1. Run the lens

```bash
source ~/.zshrc
./venv/bin/python evaluate_strategy.py <id> --split 2>&1 \
  | grep -viE 'NotOpenSSL|Injected|Fetching|SettingWith' | tail -80
```

**Always pass `--split`** — it splits long-leg from short-leg P&L. A balanced
long/short *bar count* hides a dead leg that the aggregate WF/HO cannot see, and
that pattern has killed two gold candidates. Add `--book-corr` to force full-book
correlation when same-instrument incumbents exist.

This prints the DB record, the look-ahead gate verdict, the real-sized
reconstruction (directionality / Sharpe / concentration / per-year / recent / maxDD),
and the curation block. It already handles `best_params`, the stop-path positional
index, pair archetypes (`instrument2`), and macro/calendar injection. Only hand-write
a reconstruction for something it doesn't cover (hedge hypotheses, custom windows) —
`dd_salvage.py` is the template.

## 2. Sanity-check the reconstruction before trusting it

If the output shows **all-zero returns**, or a **wildly negative Sharpe against a DB
PASS** — suspect the harness, not the strategy. Two bugs, both of which have caused
a wrong swap that had to be backed out:

- **Params.** Use `validation_results.best_params`, never the first value of each
  `param_grid` list. Taking `v[0]` gives the wrong param set *and* silently drops
  `stop_mult` (which only exists in `best_params`), so `compute_net_strategy_returns`
  falls back to the legacy no-stop path. Wrong params + no stop = a fabricated
  return stream.
- **Index type.** When `stop_mult` is present, `compute_returns_with_stop` returns
  `pd.Series(out[1:]).reset_index(drop=True)` — length n−1, positional RangeIndex,
  aligned to `df.iloc[1:]`. A later `net.reindex(df.index)` against a DatetimeIndex
  turns every value into NaN→0 and the sleeve reads as flat 0%. Fix before
  reindexing:
  ```python
  if not isinstance(net.index, pd.DatetimeIndex):
      net.index = df.index[1:len(net) + 1]
  ```
  The no-stop path is datetime-indexed, so this only bites the *correct* path.

## 3. The six checks

1. **Real WF/HO edge, clean torture.** WF ≥ 0.5, HO > 0 with enough HO trades,
   `torture_flags == []`.
2. **Exit integrity.** Reconstruct max-hold vs the intended exit. Catches bugs like
   `elif a>0 & a<9:` (bitwise precedence → always true → never exits).
   *Gotcha:* `dd_salvage.holding()` counts consecutive in-market bars **across
   long↔short flips**, resetting only on a flat bar — so an always-in two-sided
   oscillator shows a huge max_hold that is not one stuck position. RLE the position
   series **per direction** before calling an exit broken. Only a single-direction
   run far exceeding the intended hold is a real bug.
3. **Directionality.** Reject one-sided or >60% net-long, especially crypto and
   equity indices — structural drift makes long-only a beta harvest, not an edge.
4. **Rally/crash concentration.** Top-2-year share of positive log return. >50–60%
   is regime beta. Rejected crypto candidates ran 66–84%.
5. **Look-ahead.** A static code read is **not** enough. The validator now runs
   `truncation_lookahead_flip_rate` (recomputes the signal on `data.iloc[:t+1]` for
   the last 120 bars, fails above 5%), but it **under-reads on selective sleeves** —
   it divides flipped bars by *all* sampled bars, so a sleeve in-market 4% of the
   time can pass at 4% flip and still have 100% of its edge vanish under causal
   replay. When Sharpe > ~3, net ≥ gross, every year positive with large magnitudes,
   or the code writes to past `pos[]` indices from a loop over later bars, rank the
   *severity* with a causal replay:
   ```bash
   ./venv/bin/python causal_audit.py --sids <id> --csv /tmp/causal.csv 2>&1 | tail -30
   ```
   **`causal_audit.py` only covers sleeves already at `status='paper_trading'`** — it
   selects that set and then filters by `--sids`, so passing a candidate's id yields
   an empty run. It answers *retire-or-keep* for a deployed sleeve, not *deploy-or-not*
   for a candidate. For a candidate, the truncation flip rate from `evaluate_strategy.py`
   is the available signal; to get a collapse number you must replay it by hand
   (`sig[t] = fn(df[:t+1]).iloc[-1]` over a window containing enough entries) — and its
   `--csv` defaults to `causal_audit.csv` in the repo root, so redirect it.

   Read the **collapse**, not the flip rate: ~100% collapse = the edge *is* the
   look-ahead (retire, don't re-validate — scan-and-fill is unsalvageable); ~33% =
   partial real edge, borderline; ~0% = causal and clean. The window is sized per
   sleeve to contain a minimum number of entries, because a fixed bar count gives a
   4%-in-market sleeve only a handful of trades to judge. Note that flip rate does
   not rank harm — a benign full-sample normalisation flips *more* than a harmful
   retroactive edit. Any flip >5% still disqualifies a candidate, because the
   validated signal is not the signal that would trade live.
6. **Correlation vs the live book.** Compute it — don't reason about it from
   instrument names. A new *instrument* can still be correlated in *returns*, and
   two co-moving instruments (DAX vs SPX) can be uncorrelated at the strategy level
   if the timing differs. `run_portfolio.sh` also prints the matrix and flags pairs
   >0.5. <0.3 = genuine diversifier.

## 4. Curate — don't accumulate

When the candidate is in a cluster the book already covers, adding it deepens
concentration. Compare head-to-head against the incumbents:

| Candidate vs incumbent | Action |
|---|---|
| Dominates **and correlated** | **Swap** — retire the loser, deploy the winner. Cluster count stays flat, quality rises. |
| Dominates **and uncorrelated** | **Keep both** — weight the winner up, trim the loser. Don't throw away free diversification. |
| Doesn't dominate | **Reject**, even if it passed every gate. |
| New instrument class, max \|corr\| < 0.3 | **Add** — a genuine diversifier earns its slot. |

Retiring a **flat** sleeve is `retire_strategy(sid)` alone. A sleeve **holding units**
must be flattened first — see `deploy.md`.

## 5. Tie-breaker for a marginal candidate

WF/HO are full-history composites and stay positive while a strategy loses money in
the *current* regime. For an index or equity candidate with negative recent per-year
returns, the real-sized recent slice decides it:

```bash
./venv/bin/python oanda_book_simulator.py --start <recent> --end <today> \
  --risk 0.005 --max-risk 0.02 --csv /tmp/cand.csv
```

Beware: **the trailing 12 months is in-sample** — sleeves were generated ~May–June
2026 and optimised over that window. Use the locked-holdout slice for a quasi-OOS
read, and treat even that as flattered (it drove pass/fail selection).

The min-units clip binds at ~1 unit on index sleeves, so you cannot size an index
sleeve down out of trouble — its risk is roughly take-it-or-leave-it.

## Traps

- **High Calmar or holdout alone is not edge.** On crypto or an index during a bull
  run it is beta in a favourable regime. Most drawdown-gate-blocked "edges" are beta.
- **A low DSR in `final_status` is descriptive, not a red flag.** The annotation
  deflates full-sample *Sharpe* against the search's expected-max Sharpe, but the
  pipeline selects on *GT-score* — a mismatched axis, so DSR runs systematically low
  for any GT-selected winner. Overfit is controlled by the locked holdout, not DSR.
  `DSR_GATE` stays off. Read raw annualised Sharpe as the quality signal instead.
- **Low average correlation still concentrates in the tail.** Same-instrument sleeves
  all go long together in a rally regardless of their pairwise corr.
- **Long-only + a regime gate self-hedges.** Before accepting an active short leg as
  "insurance", require it to be standalone positive or at least flat. A short book
  that only pays in one historical decline and bleeds in bull years is decoration.
