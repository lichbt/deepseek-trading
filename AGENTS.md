# AGENTS.md

Agent instructions for this repo live in `CLAUDE.md` (binding decisions, session-boot state,
sleeve ops). Read it first. This file is the entry point for agents that look for `AGENTS.md`,
and carries the deploy section so a session can ship a book change without digging through the
skill reference.

## Deploy a book change (one command)

**Zeabur is production for the prop book** (cutover 2026-07-27); execution is the cTrader Open
API. The Mac runs research and the OANDA paper book only.

Change the book in the working DB first (`retire_strategy(id, reason)`, or
`start_live_trading(id)` to promote a passed sleeve), then from the repo root:

```bash
./scripts/deploy_book.sh --reason "what changed and why"
```

It recomputes weights, builds the compact deploy DB, commits on `feature/ctrader-adapter` in the
`.podwt` worktree (blob staged via `hash-object` + `update-index`, so the 485 MB working DB can
never be committed), then `interlock on` -> 0 pods -> push -> poll image -> `reset-db` -> `off` ->
one pass -> verify. A trap releases the `pods=0` quota on ANY failure, it refuses to run inside the
00:05-00:35 UTC pass window, and `--dry-run` changes nothing.

Rails:

- Never `git add pipeline.db` - only the hash-staged blob holds the compact DB.
- Never commit `fix_runner_state.json`: per-host broker state.
- `reset-db` (NOT `reset-volume`) after a book change, or the volume keeps serving the OLD book.
- One `fix_runner` per account: `interlock on` and 0 pods before any push.
- No pty? `scripts/zeabur_interlock.sh` falls back to paramiko (`scripts/pod_remote.py`).
- Verify: no retired id in pod `state`, `loaded N cTrader-tradeable sleeves` in the log,
  `trigger-status` receipt `ok: true`, one `fix_runner`.

Full procedure and manual fallback: `.claude/skills/sleeve-ops/references/deploy.md`.

## Sleeve operations — always use the skill

`.claude/skills/sleeve-ops/` **is the entry point for evaluating, stress-testing, deploying and retiring sleeves. Invoke it; do not re-derive any of this inline or from memory.** Three modes over one pipeline, each with its own reference — read only the one for the mode you're in:

| Ask                                                       | Mode       | Reference                  |
| --------------------------------------------------------- | ---------- | -------------------------- |
| "check this strategy `<id>`", swap-or-add, retire-or-keep | evaluate   | `references/evaluate.md`   |
| prop sizing, DD odds, "can we scale up", pass probability | montecarlo | `references/montecarlo.md` |
| "deploy it", "retire `<id>`", "push to Zeabur"            | deploy     | `references/deploy.md`     |

A full candidate review runs all three: evaluate → re-run montecarlo because the sleeve count changed → deploy.

### Verifying a strategy (evaluate mode)

`references/evaluate.md` holds the 6-check lens, the swap / keep-both / reject table, and **two reconstruction gotchas that have each produced a wrong deploy decision**. That is the reason to invoke it rather than improvise a check.

- **Reconstruct, don't assert.** Every claim about a strategy's behaviour must come from a run made in this session — never from stored scores, a previous session, or this file.
- **Evaluation is read-only** and needs no confirmation. Deploy and retire need an explicit "yes" naming the sleeve.
- `causal_audit.py` **cannot answer deploy-or-not.** It takes `--sids` but iterates `status='paper_trading'` *before* filtering, so passing an undeployed candidate's id yields an empty run. It answers retire-or-keep for a live sleeve only.
- **Never quote a stale number.** Deployed risk, worst day, sleeve count and cluster caps all move whenever the book changes — recompute them (SKILL.md lists the commands) instead of carrying a value in.
- **Never pin an end date** for decay checks. `evaluate_strategy.FULL_END` was once hard-coded and silently scored later runs on stale data.

