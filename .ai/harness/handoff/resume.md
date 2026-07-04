# Codex Resume Packet
<!-- generated-by: repo-harness codex-handoff-resume v1 -->

> **Generated**: 2026-06-29 23:06:27
> **Reason**: repo-harness-adopt-verify
> **Working Directory**: /e/tool-crawl-scimago

## Resume Prompt

You are starting a fresh Codex session for an existing long-running task. Do not rely on prior chat history or Codex auto-compact. First read the source artifacts listed below, then continue from the exact next step in the repo handoff.

Current prompt files first:
- If the current user message lists files under `# Files mentioned by the user`, references `pasted-text.txt`, or includes an explicit attachment/file path, read those current-input files before the repo recovery artifacts below.
- Use handoff, resume, and `tasks/current.md` as recovery context only; they do not outrank the current user message.

Required first reads:
- AGENTS.md
- .ai/harness/handoff/current.md
- tasks/todos.md
- (none)
- docs/researches/
- .ai/harness/checks/latest.json

Conditional first reads:
- Active plan: (none)
- Active contract: (none)
- Implementation notes: (none)
- Global handoff: /c/Users/LENOVO/.codex/handoffs/handoff-260629.md

Execution rules:
- Treat filesystem artifacts as the source of truth.
- Decide in the main agent whether to use subagents, parallel sidecars, sidecar `codex exec --json`, or a bounded main-thread trace for broad research/log scans based on context impact and callable tools; do not ask the user for spawn confirmation.
- Keep deep research conclusions in `docs/researches/`, not only in chat.
- Do not run `/compact` as the primary recovery path.
- Preserve the current dirty worktree and do not touch unrelated untracked files.

## Source Artifacts

- Repo handoff: .ai/harness/handoff/current.md
- Resume packet: .ai/harness/handoff/resume.md
- Checks: .ai/harness/checks/latest.json
- Todo: tasks/todos.md
- Research: docs/researches/
- Plan: (none)
- Contract: (none)
- Notes: (none)
- Global handoff: /c/Users/LENOVO/.codex/handoffs/handoff-260629.md
