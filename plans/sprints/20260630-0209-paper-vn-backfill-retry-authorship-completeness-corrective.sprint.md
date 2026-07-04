---
title: "Paper VN Retry and Authorship Completeness Corrective Sprint"
kind: "sprint"
created_at: "2026-06-29T19:09:11.839Z"
source: "repo-harness-mcp"
---
# Paper VN Retry and Authorship Completeness Corrective Sprint

> **Status**: Draft

## Source

- PRD: `plans/prds/20260630-0208-paper-vn-backfill-retry-authorship-completeness-corrective.prd.md`

## Execution Rule

- Execute task cards in order.
- Keep each task card reviewable as one staged slice.
- After every completed phase, update the checklist and stage the result before continuing.
- Do not treat unstaged work as a completed phase.

## Checklist

### Task Card 1: FIX-01 — Baseline and failing regression tests

- [x] Objective: Confirm the existing worktree branch and dirty state, then add focused tests that fail against the current implementation for retry persistence/consumption, queue cleanup, existing-author missing-link repair, dry-run planned repair classification, and Article-4-style partial authorship completeness. Do not change production code until the failures are demonstrated.
- [x] Files/entrypoints: `scratch/papervn_worktree/.git or root worktree metadata`, `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_affiliation_import.py`
- [x] Verification: `Confirm branch hao/feature/paper-vn`, `Record git status --short`, `Run current test_vn_* baseline`, `Add failing tests for all five identified runtime gaps`, `Confirm tests use mocks/SQLite/temp files only`
- [x] Stage gate: Stage only regression tests and baseline evidence once the new tests fail for the expected reasons.

### Task Card 2: FIX-02 — Persist, consume and reconcile retry queues

- [x] Objective: Make retryable outcomes durable before exit, without advancing last_scanned_article_id. On --resume, process retryable_article_ids before forward scanning. Remove each article ID from all queues before assigning its latest outcome so SUCCESS and transitions cannot leave stale IDs.
- [x] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `RETRYABLE writes checkpoint v2 immediately`, `last_scanned_article_id remains the prior successful/scanned ID`, `Resume consumes retryable IDs before select_articles forward scan`, `Successful retry removes the ID from all queues`, `Still-retryable outcome remains queued`, `Transition RETRYABLE→UNAVAILABLE/FAILED moves the ID to exactly one correct queue`, `No retryable record is silently dropped`
- [x] Stage gate: Stage retry/checkpoint changes after focused queue tests pass.

### Task Card 3: FIX-03 — Repair existing-but-unlinked authors safely

- [x] Objective: When --repair-authorships is enabled, always evaluate whether the resolved OpenAlex Author is linked to the current Article. Link an existing safe Author when Author_Article is missing; upsert and link only when the Author does not exist. Preserve author_position and never repair by ambiguous name fallback.
- [x] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tools/vn_journals/paper_vn_affiliations.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `Existing Author with matching OpenAlex ID but no Author_Article gets linked in execute mode`, `Dry-run reports link_existing_author without writes`, `Already-linked Author creates no duplicate`, `New Author can still be safely upserted and linked`, `Conflicting OpenAlex identity is not merged`, `author_position is preserved`
- [x] Stage gate: Stage authorship repair changes after focused repair tests pass.

### Task Card 4: FIX-04 — Correct dry-run repair classification

- [x] Objective: Separate planned, safely resolvable repairs from genuinely unresolved authors. A dry-run with valid OpenAlex identity and a planned repair must not add the same author to unresolved_authors or force AUTHOR_UNRESOLVED solely because no write occurred.
- [x] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `Planned repair appears in planned_author_repairs`, `The same planned author is absent from unresolved_authors`, `Dry-run outcome communicates PLANNED_REPAIR or another documented non-error state`, `Unresolvable/ambiguous identity remains AUTHOR_UNRESOLVED`, `Dry-run performs no Author/Author_Article/Institution writes`
- [x] Stage gate: Stage dry-run classification separately after focused tests pass.

### Task Card 5: FIX-05 — Author-level completeness for incomplete-authorship mode

- [x] Objective: Add explicit author-level completeness measurement before and after processing. In --incomplete-authorships mode, do not use article_has_exact_year_affiliation as the success criterion. Report linked author count, complete/incomplete counts, and source-unavailable authorships or equivalent details.
- [x] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`, `scratch/papervn_worktree/docs/paper-vn-backfill-operator-checklist.md`
- [x] Verification: `Article with 3 linked authors and only 1 exact-year affiliation starts with incomplete_count=2`, `After processing, SUCCESS requires incomplete_count_after=0 in incomplete-authorship mode`, `If source has no institutions for remaining authors, outcome is explicit PARTIAL/UNAVAILABLE rather than SUCCESS`, `Article-level --only-missing behavior remains unchanged`, `Report includes before/after author completeness fields`, `No use of last_known_institution`
- [x] Stage gate: Stage completeness logic and docs after Article-4-style tests pass.

### Task Card 6: FIX-06 — Full verification and closeout synchronization

- [x] Objective: Run compile, focused tests and full test_vn_* suite. Update worktree and repo-root handoff/check evidence consistently, resolve sprint checkbox inconsistencies, and document safe operator commands. Do not run a full production backfill, commit or push.
- [x] Files/entrypoints: `scratch/papervn_worktree/.ai/harness/handoff/current.md`, `.ai/harness/handoff/current.md`, `.ai/harness/checks/latest.json`, `plans/sprints/20260630-0209-paper-vn-backfill-retry-authorship-completeness-corrective.sprint.md`, `scratch/papervn_worktree/docs/paper-vn-backfill-operator-checklist.md`
- [x] Verification: `Python compile passes`, `Focused regression tests pass`, `python -m unittest discover -s tests -p "test_vn_*.py" -v passes`, `CLI --help still works`, `git diff --cached --check passes`, `Root and worktree handoffs agree on implementation status`, `checks/latest.json records actual commands/results`, `No production checkpoint/report, .env, cache, pyc or dump is staged`, `No full production backfill, commit or push occurred`
- [x] Stage gate: Stage final documentation and evidence only after exact verification is complete.

## Final Acceptance

- [x] All task cards are checked.
- [x] Required checks pass.
- [x] Handoff explains staged state, residual risks, and next bottleneck if any.
