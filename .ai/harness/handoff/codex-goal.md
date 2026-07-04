---
title: "Codex Goal"
kind: "codex-goal"
created_at: "2026-06-29T19:09:21.020Z"
source: "repo-harness-mcp"
---
# Codex Goal

## Source of truth

- PRD: `plans/prds/20260630-0208-paper-vn-backfill-retry-authorship-completeness-corrective.prd.md`
- Checklist Sprint: `plans/sprints/20260630-0209-paper-vn-backfill-retry-authorship-completeness-corrective.sprint.md`

## Role

Codex is the executor. ChatGPT/repo-harness may prepare planning artifacts, but implementation ownership stays in the local Codex session.

## Scope

- Open or use an isolated worktree for the sprint implementation.
- Execute the checklist Sprint task cards in order.
- Update the Sprint checklist as phases complete.
- Stage each completed phase before continuing to the next phase.
- Do not modify the reference repo or ignored secrets/ops state.

## Required workflow

1. Read the PRD and Sprint paths above before editing.
2. Build the P1/P2/P3 map required by repo-local AGENTS.md for non-trivial changes.
3. Execute one checklist task card at a time.
4. After each phase, run the relevant focused checks, update the checklist, and stage the completed slice.
5. Continue until the Sprint checklist is complete or a real blocker is reached.
6. Leave a concise handoff with staged state and verification evidence.

This is a narrow corrective sprint after review of the previous implementation. Work only inside existing worktree scratch/papervn_worktree on branch hao/feature/paper-vn. Do not checkout at repository root and do not create/remove/prune worktrees. Preserve all unrelated dirty changes. Start by writing regression tests that fail against the current code before changing production code. Fix these exact issues: (1) RETRYABLE currently adds an ID to memory but does not persist checkpoint before exit, retry queues are loaded but never consumed on resume, and stale IDs are not removed when status changes; (2) --repair-authorships only runs when resolve_author_id returns None, so an existing Author without Author_Article is not repaired; (3) dry-run planned repair is incorrectly also classified unresolved; (4) --incomplete-authorships selects correctly but still uses article-level exact-year affiliation to report SUCCESS, which is wrong for partially affiliated articles such as the audited Article 4. Add author-level before/after completeness fields and explicit partial/source-unavailable outcomes. Do not rework the already-correct main importer, do not modify FE/BE/schema, do not call production DB or live APIs in automated tests, do not run a full production backfill, stage each phase explicitly, and do not commit or push. Synchronize both worktree and repo-root handoff/check artifacts at closeout with real command evidence.

## Required checks

- Run the checks named by the Sprint task card.
- At sprint closeout, run repo-required checks unless the Sprint narrows the verification surface with a stated reason.

## Done when

- The checklist Sprint is complete.
- Every completed phase is staged.
- Checks pass or failures are documented with exact blocker evidence.
- No commit is created unless the user explicitly asks for commit.

## Host-native /goal prompt

```text
/goal
Read: plans/prds/20260630-0208-paper-vn-backfill-retry-authorship-completeness-corrective.prd.md
Open or use a worktree and complete: plans/sprints/20260630-0209-paper-vn-backfill-retry-authorship-completeness-corrective.sprint.md
After each completed phase, stage the result before continuing.
Use the user's language for status reports unless repo-local instructions require otherwise.
```
