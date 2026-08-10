# A policy-flat state in order_decision

Type: task
Status: open
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
