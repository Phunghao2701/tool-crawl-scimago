# Repo Agent Context

This is the root routing contract for Claude Code and Codex.

## Root Workflow Contract

- Keep sibling `CLAUDE.md` and `AGENTS.md` files aligned. Claude Code consumes `CLAUDE.md`; Codex consumes `AGENTS.md`.
- Treat `docs/spec.md` as stable product truth, `tasks/current.md` as a derived status snapshot, and `tasks/todos.md` as the deferred-goal ledger; current execution stays in the active plan's `## Task Breakdown`.
- Treat `docs/researches/`, `tasks/lessons.md`, and `.ai/harness/policy.json` as durable workflow context.
- Use `.ai/context/context-map.json` and `.ai/context/capabilities.json` to discover functional-block contracts.
- Do not infer local `CLAUDE.md` or `AGENTS.md` files from broad physical layouts such as `apps/*`, `packages/*`, or `services/*`.
- Put capability-specific ownership, entrypoints, and verification commands in explicitly selected functional-block contracts.
- Keep root context concise; route deep implementation detail into plans, task notes, research, workstreams, or architecture docs.
- Treat `_ref/` as ignored external reference material and `_ops/` as ignored local operations state.
- Prefer repo-local workflow artifacts over tool-specific chat memory.

## Pipeline Monitoring Memory

- For ETL monitoring or resume work, close every terminal event (`goal_reached`, `budget_exhausted`, `user_stopped`, or `failed`) and explicit session wrap-up with a durable Obsidian handoff before the final response.
- Resolve the active vault from `OBSIDIAN_VAULT_PATH` when set; otherwise read `%APPDATA%/obsidian/obsidian.json` and select the vault marked `open: true`. Read that vault's `AGENTS.md` before writing.
- Create a new UTF-8 note under `memory/conversations/` named with the date/time and project; never overwrite an existing conversation note. Record the objective, verified stats, process/lock state, checkpoints, relevant logs, code changes, decisions, unresolved items, and next step.
- Keep API keys, database credentials, and `.env` contents out of second-brain notes. Clearly distinguish directly verified facts from conversation-reported or inferred state.
