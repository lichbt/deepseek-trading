# CLAUDE.md — deepseek-oanda-trading

## Binding decisions (loaded at session boot)

Rules already decided for this pipeline — honor them, don't relitigate. The
GT-Score gates, one-shot validation, and design constraints are load-bearing.
Generated from the Second Brain; to add one, use `brain.py decision` (below).

@/Users/lich/secondbrain/projects/deepseek-oanda-trading/DECISIONS.md


## Where we left off (read this first)

@/Users/lich/secondbrain/projects/deepseek-oanda-trading/HANDOFF.md

In-flight state from the last session: what was being worked on, the next step,
and anything half-finished. It is overwritten every session — trust it as
"where we stopped", not as history. Decisions above are the settled rules.

**Before you finish a session, update it:**

```bash
/Users/lich/secondbrain/.venv/bin/python /Users/lich/secondbrain/brain.py \
    handoff --project deepseek-oanda-trading "$(cat <<'EOF'
**Working on:** <thread in flight>
**Done this session:** <what landed>
**Next step:** <single next action, file:line if known>
**Blocked / open:** <waiting on a decision or answer>
**Careful:** <anything that would bite someone resuming>
**Ruled out:** <hypotheses tested and DISPROVED, separated by ';'>
**Anchors:** <literal ids to search on: file.py:40, FUNC_NAME, env vars, shas>
EOF
)"
```

`Working on` / `Next step` / etc. are REPLACED each write. `Ruled out` and
`Anchors` CARRY FORWARD — eliminations are what a multi-session hunt loses, and
anchors are how the next session searches further back. Ruled-out items expire
after 30 days from the loaded file but stay in `HANDOFF-log.md` forever.

To see further back:

```bash
brain.py resume --project deepseek-oanda-trading --history 3   # last 3 superseded handoffs
brain.py find "SOME_IDENTIFIER"             # literal search — query is semantic
                                            # and misses exact ids
```

### The queue behind the handoff

`Next step` is the head of a queue kept in the brain's
`projects/deepseek-oanda-trading/context.md` under `## Backlog`:

```markdown
- [ ] open item
- [~] waiting item — waits on: <the specific thing that unblocks it>
- [x] done
```

A waiting item MUST name its unblock trigger, or it silently rots. Done items
stay as a record and are never loaded.

```bash
brain.py tasks --project deepseek-oanda-trading   # this project's open + waiting
brain.py tasks --waiting                          # everything blocked, all projects
```

This repo also has its own `BACKLOG.md`. Boundary: repo backlog = implementation
tasks meaningful only inside this codebase; brain backlog = work that survives a
session and that a cold future session needs. If it would still matter to someone
who had forgotten this codebase, it belongs in the brain.

Keep the body under 2000 chars — it is boot-loaded every session. A SessionEnd
hook writes one automatically if you forget, but yours is better: you know the
work first-hand, and the hook only sees the transcript.

## Second Brain (durable decision context)

This repo is wired to the Second Brain at `/Users/lich/secondbrain`. Consult it
for durable decisions and cross-project knowledge — **not** code-level details,
which live in this repo's own docs (ARCHITECTURE.md, CONFIGURATION.md,
FILE_STRUCTURE.md, BACKLOG.md).

Query it before making research/validation/design decisions:

```bash
/Users/lich/secondbrain/.venv/bin/python /Users/lich/secondbrain/brain.py \
    query "<your question>" --project deepseek-oanda-trading --json
```

Add `--all` to check whether another project (e.g. trading-pipeline) solved it.

## Writing back to the brain

When a durable decision or constraint is made — a validation-gate change, a
design rule, a metric change, a broker/data choice, a direction change — save it
so future sessions honor it:

```bash
/Users/lich/secondbrain/.venv/bin/python /Users/lich/secondbrain/brain.py \
    decision --project deepseek-oanda-trading "decided X because Y"
```

Save durable decisions only — never code changes, candidate JSON, run results, or
`pipeline.db` state (those are this repo's own concern, not the brain's).

**Keep each decision under ~600 characters — one or two sentences.** The `@`-ref
above boot-loads every decision into every session here, so a decision is paid for
on each run, forever. Write *the rule and its reason*, not the run that produced it:

- ✅ `swap makes the .t symbols mandatory for the prop book — plain listings lose
  ~3/4 of the edge once rollover is charged`
- ❌ a full paste of simulator output, spread tables, and tick samples

When the measurement matters, record the conclusion and let the evidence spill:
anything over 600 chars is automatically moved to
`/Users/lich/secondbrain/projects/deepseek-oanda-trading/evidence/<date>-<slug>.md`
and replaced by a headline + link. Nothing is lost, and the boot cost stays flat.
Pass `--no-spill` only when the full body genuinely must sit inline.

Not hypothetical: on 2026-08-03 the loaded file had reached 189KB — roughly 47k
tokens charged before any work began — because 74 decisions averaged 2.5k chars
each. Retro-fixed with `brain.py migrate-decisions`.

## Sleeve operations — always use the skill

**`.claude/skills/sleeve-ops/` is the entry point for evaluating, stress-testing,
deploying and retiring sleeves. Invoke it; do not re-derive any of this inline or
from memory.** Three modes over one pipeline, each with its own reference — read
only the one for the mode you're in:

| Ask | Mode | Reference |
|---|---|---|
| "check this strategy `<id>`", swap-or-add, retire-or-keep | evaluate | `references/evaluate.md` |
| prop sizing, DD odds, "can we scale up", pass probability | montecarlo | `references/montecarlo.md` |
| "deploy it", "retire `<id>`", "push to Zeabur" | deploy | `references/deploy.md` |

A full candidate review runs all three: evaluate → re-run montecarlo because the
sleeve count changed → deploy.

### Verifying a strategy (evaluate mode)

`references/evaluate.md` holds the 6-check lens, the swap / keep-both / reject
table, and **two reconstruction gotchas that have each produced a wrong deploy
decision**. That is the reason to invoke it rather than improvise a check.

- **Reconstruct, don't assert.** Every claim about a strategy's behaviour must come
  from a run made in this session — never from stored scores, a previous session, or
  this file.
- **Evaluation is read-only** and needs no confirmation. Deploy and retire need an
  explicit "yes" naming the sleeve.
- **`causal_audit.py` cannot answer deploy-or-not.** It takes `--sids` but iterates
  `status='paper_trading'` *before* filtering, so passing an undeployed candidate's
  id yields an empty run. It answers retire-or-keep for a live sleeve only.
- **Never quote a stale number.** Deployed risk, worst day, sleeve count and cluster
  caps all move whenever the book changes — recompute them (SKILL.md lists the
  commands) instead of carrying a value in.
- **Never pin an end date** for decay checks. `evaluate_strategy.FULL_END` was once
  hard-coded and silently scored later runs on stale data.

### FIX sleeve deployment

Full procedure: `.claude/skills/sleeve-ops/references/deploy.md`. Invoke the skill
rather than working from this summary.

1. Activate it as `paper_trading`.
2. **Restart the paper book AND roll the Zeabur pod.** `com.lich.papertrading` runs
   locally; the prop book is a Zeabur pod (`com.lich.fixtrading` on the Mac is disabled
   as `.plist.disabled`). Each freezes its sleeve list at process start, so until both
   restart a new sleeve never trades and a retired one keeps trading (and re-enters on
   its next flip).
3. Rebuild `portfolio_state.json`, then re-check deployed risk — cluster caps don't
   renormalise, so changing the sleeve count moves every remaining position.
4. Build a compact `pipeline.db` with `scripts/build_deploy_db.py --no-units`
   (`paper_trading` rows plus matching `validation_results` / `live_status`).
5. Never replace or commit the large local research `pipeline.db`.
6. Stage the compact DB from a temporary file with `git hash-object` and
   `git update-index --cacheinfo`, leaving the working DB untouched.
7. Commit only `pipeline.db`, `portfolio_state.json`, and `portfolio.py` when its
   conviction multiplier changed. Never stage unrelated working-tree changes.
   **Never commit `fix_runner_state.json`** — it is per-host live broker state, and
   a shipped copy makes a fresh volume claim positions it doesn't own.
8. Verify: state `pos_id` count equals `broker_positions=N` in the log; every open
   position carries a broker-side `stopLoss` with **zero** standalone stop orders.
   Repair mismatches from real PosIDs, never from a count.

**Zeabur is production for the prop book; the Mac is paper trading and research**
(since the 2026-07-27 cutover). Execution runs over the **cTrader Open API**, not FIX
— `VENUE=ctrader` in the pod's env; `VENUE` defaults to `fix`, so rollback is unsetting
it, never a code revert. Stops are ATTACHED to the position, and closes are by
`positionId`.

Exactly **one `fix_runner` may run per broker account** — both hosts share the same
account, so two runners trade it blind to each other and double the effective size.
The Mac's job must stay `~/Library/LaunchAgents/com.lich.fixtrading.plist.disabled`.

**`git push` to `feature/ctrader-adapter` is a TRADING ACTION**: it auto-deploys, and
`fix_runner` runs a full trading pass on startup. Worse, with `replicas=1` the default
RollingUpdate starts the new pod BEFORE killing the old, so a plain push briefly runs
TWO runners. Always `./scripts/zeabur_interlock.sh on` -> confirm **0 pods** -> push ->
`off`. The push itself resets `spec.replicas` to 1; only the `pods=0` ResourceQuota
holds it down.

The mounted `/data/pipeline.db` is only seeded when absent, so no push refreshes a live
volume's book — use `./scripts/zeabur_interlock.sh reset-db` (deletes only the DB).
Never `reset-volume` unless the account is FLAT: it wipes state too, and a pod that
forgets its positions re-enters every sleeve and doubles the book.
