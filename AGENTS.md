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
