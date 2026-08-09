# Steering — generation knobs you can edit by hand

This file steers **what gets generated**. It is read fresh at the start of every
batch, so an edit takes effect on the **next** batch with no restart and no code
change. Delete the file, or empty the block, and generation falls back to its
built-in defaults — nothing here is required.

Everything lives in the single fenced YAML block below. Edit that; the prose
around it is for you, not the parser.

## Why this file exists

`meta_review` writes a prose directive into `thesis.md` after each batch, and the
thesis model reads it — but **prose cannot move instrument or timeframe
selection**, because those are chosen in Python *before* the model sees the
prompt. Measured 2026-08-03 over 922 generations: the directive said *"Focus on
XAU_USD and NZD_USD"* and XAU_USD came out the **second-lowest of 31**
instruments (26 gens vs a 30 mean). The directive also said *"avoid H1/H4"* while
the scheduler forced them onto 40% of every batch.

So: knobs that the **scheduler** obeys go here. Advice for the **model** stays in
`thesis.md`.

## The knobs

**`focus_instruments`** — instruments to over-sample. These do NOT replace the
round-robin; they get a bounded share of slots (`focus_slot_every`), so the rest
of the pool keeps its coverage. Leave the list empty to disable.

Keep this bounded on purpose. Chasing instruments that recently scored well is a
mild overfit pressure at the portfolio level, and the standing rule is that a
candidate's trailing window is in-sample by construction. 10% is a nudge; 50%
would be a strategy.

**`focus_slot_every`** — one focus slot per N non-wild iterations. `10` ≈ 2 slots
of a 20-batch (10%). Raise the number to weaken the bias, lower it to strengthen.
Set `focus_instruments` empty to turn it off entirely.

**`timeframe_rotation`** — the forced-timeframe cycle, one entry per iteration,
wrapping. Wild and calendar/event/academic slots override this (they pin their own
timeframe), so the realised mix is close to but not exactly these proportions.

⚠ **Measure these rates on the HONEST ERA only.** The all-time figures previously
quoted here — D 0.291%, H4 0.258%, H1 0.047%, W 1/3632 — reproduce correctly, but
they average across the 2026-06-10 macro publication-lag fix and that flatters
intraday enormously. Split by era (re-measured 2026-08-09, `final_status LIKE
'PASS%'`, which is the right numerator — `strategies.status` drops everything that
passed and was then DEPLOYED):

| tf | pre-fix (leaked) | post-fix (honest) |
|----|------------------|-------------------|
| D  | 49/10,607 = 0.462% | **92/38,727 = 0.238%** |
| H4 | 32/2,337 = **1.369%** | **1/11,452 = 0.009%** |
| H1 | 4/2,740 = 0.146% | 2/10,362 = 0.019% |
| W  | 0/787 | 1/3,149 = 0.032% |

H4 passed at 3x daily while macro was leaking, and at 1/26th of daily once it was
not — the leak favoured intraday macro most, which is exactly what
`HONEST_ERA_START` was introduced for. At daily's honest rate, 11,452 H4 gens
should have produced ~27 passes; they produced 1.

So H4 is cut to a 5% tail rather than dropped outright: enough to notice if
intraday ever starts working, small enough that it is not spending a fifth of
every batch on a leak artifact. Restore it only against honest-era evidence.

**`avoid_instruments`** — dropped from the rotation entirely. Use sparingly; an
instrument with no strategies is an instrument you can never deploy. Empty by
default.

```yaml
# ---- edit below this line ----

focus_instruments: []          # e.g. [XAU_USD, NZD_USD] — bounded over-sampling
focus_slot_every: 10           # 1 focus slot per N non-wild iterations (~10% at 10)

avoid_instruments: []          # dropped from the rotation completely

timeframe_rotation: [D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, D, H4]
# W dropped and H4 cut 20% -> 5%, 2026-08-09. W was only reaching ~2.6% of slots
# because of an index-aliasing bug in _build_batch_schedule; fixing that raised it
# to ~7%, the wrong direction. H4's "parity with daily" above is an ALL-TIME
# number and does not survive the era split — see the note below. H1 stays out.
```

## Notes

- Unknown keys are ignored, so you can leave yourself notes inside the block.
- A malformed block is **ignored with a warning**, never fatal — generation keeps
  running on defaults rather than dying overnight.
- An instrument in both `focus_instruments` and `avoid_instruments` is avoided;
  avoid wins, and the loader warns.
- Timeframes must be from `M30, H1, H4, D, W`. Invalid entries are dropped with a
  warning; if nothing valid remains the built-in rotation is used.
