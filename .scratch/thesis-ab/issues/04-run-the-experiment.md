# Run the experiment to n=147 per arm

Type: task
Status: open
Blocked by: 02, 03

## Question

Enable the controller on the live loop and let it run until **147 candidates per arm**
have reached `validation_results`, excluding dry-run candidates from 03.

Expected wall clock ~10.4h for both arms combined, but the arms finish at different times
— pro is ~1.8x slower per candidate. Balance is by COUNT: the run ends when the SLOWER arm
reaches 147, and any surplus in the faster arm is truncated to 147 by
`created_at` order (declared here, before the data exists, so the truncation rule cannot
be chosen to suit the result).

**Do not run the analysis while this is in flight.** The stopping rule is a single
evaluation at n=147/arm; an interim look destroys the α this whole design is built to
protect. Monitoring is limited to arm counts and error rates — never the endpoint.

Accepted cost, explicit: for ~10 hours, roughly half of real production generation runs on
`v4-pro`. Both arms produce genuine candidates, so nothing is thrown away, but this is
production, not a sandbox.

## Definition of done

Both arms at 147 tagged candidates in `validation_results`. Arm counts, error counts and
any excluded/untagged candidates reported. The endpoint remains uncomputed.
