# Dry run the harness on the live loop

Type: task
Status: open
Blocked by: 01

## Question

Prove the harness behaves on the real loop before committing 10 hours of production
generation to it. Unit tests in 01 prove the logic; this proves the wiring.

Run a small number of batches with the controller enabled and confirm, from real
artifacts rather than from the code:

- Both arms actually appear in the sidecar, and the model id reaching byteplus is the
  intended one per batch — verify against the request/response, not the config.
- Twin batches genuinely received the same seed and the same instrument/constraint
  schedule.
- Every candidate reaching `validation_results` has a matching sidecar row.
- The measured per-candidate cost matches the ~91s / ~163s assumption the 147/arm budget
  was sized on. **If pro is materially slower than 163s, the runtime estimate is wrong and
  the map needs revisiting before the real run.**

Also confirm the provenance stamp from 01 is present and correct on every row, and that a
deliberate mid-run edit is actually caught by 02's sha check — test the tripwire, don't
assume it.

Operational hazards to respect: the research loop runs the WORKING TREE from
`/Users/lich/deepseek-oanda-trading` (verified 2026-08-10: pid 3530 under launchd
`com.lich.autoresearch`, single loop — the second `run_forever.sh` pid is its child, not a
duplicate), and `feat/academic-recall-category` is unmerged — the harness must land on the
branch that is actually running, or it silently does nothing. This ticket touches generation only; it
does not touch the prop book or any deploy path.

## Definition of done

A short written confirmation of each bullet above, with the sidecar rows and the observed
per-candidate timings pasted in. Dry-run candidates are tagged so they can be excluded
from the real run's sample.
