# A policy-flat state in order_decision

Type: task
Status: resolved
Assignee: lich (claude session 2026-08-10)
Blocked by: —

## Question

`order_decision` today returns `'flip'`, `'align'` or `None`, and a position that is flat
with an unchanged signal stays flat — that is the whole contract. This policy needs a
position closed *deliberately* to be **reopened** on the next pass, without weakening that
contract anywhere else.

Add a `policy_flat` state and make it the ONLY thing that reopens on an unchanged signal.

Requirements settled at charting:

- **Mutually exclusive with `stopped_signal` by construction**, not by convention. A
  stop-out and a policy close are both "flat with an unchanged signal"; if the new state
  can ever describe a stop-out, the sleeve re-enters on an unchanged signal and diverges
  from the validated return stream — exactly the defect commit `58c1a6f` removed. Prefer a
  representation where the two states cannot both be set, over two independent flags with a
  rule about them.
- **Scoped to the instruments the policy covers.** Indices (`NAS100_USD`, `DE30_EUR`,
  `SPX500_USD`) for the daily rule; the selective set for the weekend rule. A sleeve
  outside both must behave exactly as it does today.
- **Fail closed.** Any ambiguity about why a sleeve is flat resolves to today's behaviour —
  stay flat. A sleeve wrongly held out for a day costs one day of signal; a sleeve wrongly
  re-entered places a real order against a stop the validation never modelled.
- **Pure decision, no I/O.** `order_decision` is a pure function and must stay one; the
  persistence is ticket 02.

Deliberately NOT in this ticket: persistence across restart (02), the pass that performs
the close (03), and anything touching `fix_runner`'s order path.

## Definition of done

Table-driven tests covering, at minimum: policy-flat + unchanged signal → reopen;
**stopped-out + unchanged signal → still None** (the negative test that matters); policy-flat
+ genuinely changed signal → normal flip, not a double-entry; a non-covered instrument
unaffected; both states somehow set → fails closed to None.

Paste real test output and the full-suite result. A self-reported pass on unrun code does
not count.

## Answer

**No new state is needed. The ticket's premise was wrong for the prop book, and the
representation it asked for already exists — better than proposed.**

`order_decision` and `stopped_signal` live in `live_test.py`, the OANDA **paper** book.
`fix_runner` — the prop pod, which is this map's only scope — has neither. It decides with
one line (`fix_runner.py:731`, now `if not acts_on_signal(sig, st)`), comparing the live
signal against `st['signal']` in `fix_runner_state.json`, and that single field already
distinguishes the two kinds of flat in exactly opposite directions:

| situation | written | next pass | behaviour |
|---|---|---|---|
| broker stop fired / closed at broker (`fix_runner.py:694`) | `FLAT(st['signal'])` | `sig == signal` | stays flat |
| soft-stop backstop (`fix_runner.py:712`) | `FLAT(st['signal'])` | `sig == signal` | stays flat |
| guard flatten — deliberate (`flatten_all`) | `FLAT(0)` | `0 -> sig` | **re-establishes** |

The mutual exclusion the ticket demanded "by construction" is therefore already
structural: **one field holding either the preserved signal or 0**, not two flags with a
rule about them. A sleeve cannot be stopped-out and deliberately-flat at once because
there is only one slot. `flatten_all`'s own docstring already states the intent — "with
the signal cleared, the next pass sees 0 -> sig and re-establishes" — it just had never
been connected to this policy.

**So a roll-flat close is: close the position, write `FLAT(0)`.** Nothing else.

### What was actually delivered

Both halves were tested where they are *used* (`test_guard_halt.py` for `FLAT(0)`,
`test_fix_runner_entry_retry.py` for `FLAT(signal)`), but nothing pinned them as a single
**contract** — which is the thing this whole effort rests on. So:

- Extracted `fix_runner.acts_on_signal(sig, st)` — a pure predicate, behaviour-identical
  to the inline comparison it replaces — so the invariant has a name and a docstring at
  the point of truth rather than in a runbook.
- Strict `st['signal']` subscript, deliberately not `.get()`: a state entry missing
  `signal` is corrupt, and `run_once`'s per-sleeve `try/except` should skip that sleeve. A
  `.get()` default would silently convert a corrupt entry into an **entry**.
- `tests/test_roll_flat_state.py` — 8 tests stating the contract, including the negative
  test that matters (stopped-out + unchanged signal must NOT act) and its positive control
  (a genuine flip after a stop still trades).

### Evidence

- `tests/test_roll_flat_state.py` — `8 passed in 0.38s`
- Full suite — `1167 passed, 11 warnings in 33.60s` (was 1159 before this ticket)

### Consequences for the map

- **02 is largely invalidated** — re-scoped from "persist a new state" to "verify the
  existing state survives a restart, and decide expiry". `st['signal']` is already written
  to `fix_runner_state.json` by both `flatten_all` and `run_once`; no migration, no new
  column. That file is per-host and must never be committed.
- **04 gets more likely to be a no-op** — if a policy close is just `FLAT(0)`, the existing
  post-roll pass should re-establish with no new code. Still must be proven from real
  artifacts, not from this reasoning.
- **The scoping requirement moved.** `acts_on_signal` is instrument-agnostic by design;
  restricting the policy to indices belongs in the close pass (03), which decides *which*
  sleeves get `FLAT(0)`. A sleeve never closed by the policy is untouched by construction.

### Not done here, by design

The close pass itself (03), the reopen proof (04), and anything scoping instruments.
Nothing is enabled: no caller writes `FLAT(0)` for the roll yet, so the pod's behaviour is
unchanged.
