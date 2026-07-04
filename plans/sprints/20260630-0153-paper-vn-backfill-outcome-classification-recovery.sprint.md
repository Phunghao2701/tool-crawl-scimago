---
title: "Paper VN Backfill Classification and Recovery Sprint"
kind: "sprint"
created_at: "2026-06-29T18:53:09.241Z"
source: "repo-harness-mcp"
---
# Paper VN Backfill Classification and Recovery Sprint

> **Status**: Draft

## Source

- PRD: `plans/prds/20260630-0152-paper-vn-backfill-outcome-classification-recovery.prd.md`

## Execution Rule

- Execute task cards in order.
- Keep each task card reviewable as one staged slice.
- After every completed phase, update the checklist and stage the result before continuing.
- Do not treat unstaged work as a completed phase.

## Checklist

### Task Card 1: TOOL-01 — Baseline, worktree safety and regression inventory

- [x] Objective: Confirm the existing worktree still targets branch hao/feature/paper-vn, record git status, read the current backfill implementation and tests, and capture the audit facts as regression context without hardcoding production IDs into business logic. Preserve every unrelated dirty-worktree change.
- [ ] Files/entrypoints: `.git/worktrees/papervn_worktree/HEAD`, `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tools/vn_journals/paper_vn_affiliations.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`, `scratch/papervn_worktree/.ai/harness/handoff/current.md`
- [x] Verification: `Confirm HEAD points to refs/heads/hao/feature/paper-vn`, `Record git status --short`, `Run existing test_vn_* suite before editing`, `Confirm no production DB or live API is contacted by tests`
- [x] Stage gate: Stage only baseline documentation or tests added for regression context; do not mix source implementation into this phase.

Evidence: `.git/worktrees/papervn_worktree/HEAD` points to `refs/heads/hao/feature/paper-vn`; baseline status was recorded with existing staged Paper VN files plus unrelated unstaged local/env/data/tool changes preserved; `python -m unittest discover -s tests -p "test_vn_*.py" -v` passed 45 tests from `scratch/papervn_worktree`.

### Task Card 2: TOOL-02 — Introduce per-article outcome model and safe report output

- [x] Objective: Create a small testable outcome model for every scanned article. Record status, reason, article identifiers, publication year, authorship count, authorships with institutions, institutions found, links inserted, unresolved authors, and whether exact-year affiliation exists after processing. Export an append-safe or atomic JSON/JSONL report to an explicit --report path without secrets.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tools/vn_journals/paper_vn_affiliations.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `Statuses distinguish SUCCESS, UNAVAILABLE_FROM_SOURCE, RETRYABLE, AUTHOR_UNRESOLVED, ARTICLE_HAS_NO_AUTHORS and FAILED or documented equivalents`, `NO_AUTHORSHIPS and NO_AUTHORSHIP_INSTITUTIONS are not counted as transient failures`, `Report contains article_id and reason but no connection string, API key or environment values`, `Report writing is atomic or safely appendable and covered by temporary-file tests`
- [x] Stage gate: Stage the outcome/report model after focused unit tests pass.

Evidence: Added `ArticleOutcome` reporting fields and JSON/JSONL report output. Focused tests covered status mapping, unavailable source reasons, no-secret report payloads, and atomic/append-safe report writes; `python -m unittest discover -s tests -p "test_vn_*.py" -v` passed 50 tests.

### Task Card 3: TOOL-03 — Upgrade checkpoint semantics and retry queues

- [x] Objective: Bump checkpoint format to a documented version that stores last_scanned_article_id rather than implying successful completion. Persist cumulative statistics and separate retryable, unavailable-source and failed article IDs or equivalent durable queues. Define old checkpoint behavior explicitly: safe migration or clear rejection with instructions. Transient failures must not be lost; unavailable-source records must not retry forever.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `Checkpoint uses last_scanned_article_id`, `429, 5xx, timeout and connection error remain retryable`, `UNAVAILABLE_FROM_SOURCE advances scan position but is recorded separately`, `Retry queue survives resume`, `Checkpoint writes atomically`, `Version-1 checkpoint handling is tested and documented`, `Resume never silently drops retryable records`
- [x] Stage gate: Stage checkpoint/retry changes after checkpoint and resume tests pass.

Evidence: Checkpoint version is now `2` with `last_scanned_article_id`, cumulative stats, and separate retryable/unavailable/failed queues. Version 1 checkpoints are rejected with migration guidance; transient records do not advance the checkpoint and retry queue persistence is tested.

### Task Card 4: TOOL-04 — Add explicit authorship repair mode

- [x] Objective: Add --repair-authorships as an opt-in mode. When OpenAlex has authorships but the article lacks matching Author_Article records, dry-run reports planned repairs; execute mode may safely upsert Author and link Author_Article with the original author_position before persisting Institution_Author. Never merge conflicting OpenAlex identities or repair by name alone when ambiguous.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tools/vn_journals/paper_vn_affiliations.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_affiliation_import.py`
- [x] Verification: `Without --repair-authorships no Author or Author_Article repair writes occur`, `Dry-run reports intended author/link repairs without writes`, `Valid OpenAlex author ID can create/link a missing Author_Article`, `author_position is preserved`, `Conflicting OpenAlex IDs are not merged`, `Article with no source authorships is classified unavailable rather than fabricated`
- [x] Stage gate: Stage authorship repair separately after opt-in, dry-run and conflict tests pass.

Evidence: Added `--repair-authorships` as an explicit opt-in. Dry-run reports planned repairs; execute mode uses OpenAlex author IDs through safe author upsert/link logic and preserves `author_position`; no-source-authorship articles classify as `ARTICLE_HAS_NO_AUTHORS`.

### Task Card 5: TOOL-05 — Support incomplete-authorship discovery

- [x] Objective: Keep article-level --only-missing behavior for Paper VN scope, and add --incomplete-authorships or an equivalent explicit mode that selects articles where at least one Author_Article lacks Institution_Author for the publication year. Avoid infinite retry for authorships whose source has no institution. Report author-level completeness separately from article-level scope completeness.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`
- [x] Verification: `Article with 1/3 affiliated authors is excluded by article-level only-missing but included by incomplete-authorships`, `Article with all authors complete is excluded`, `Author with no institution from source is classified unavailable for that authorship`, `Selection queries use publication_year exactly and never last_known_institution`
- [x] Stage gate: Stage selection-mode changes after SQL semantics tests pass.

Evidence: Added `--incomplete-authorships` as a distinct selection mode. Tests assert the SQL finds authors lacking `Institution_Author` for `a."publication_year"` exactly and does not use `last_known_institution`.

### Task Card 6: TOOL-06 — Idempotency and metadata preservation verification

- [x] Objective: Strengthen fixture tests to prove rerunning the same main articles and backfill inputs creates no duplicate Article, Author, Institution, Author_Article or Institution_Author rows, preserves non-decreasing citation/reference metrics, citation history objects and OA unknown semantics. Add a safe operator checklist for rerunning the five audited articles; do not run production automatically.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tests/test_vn_affiliation_import.py`, `scratch/papervn_worktree/tests/test_vn_article_identity.py`, `scratch/papervn_worktree/tests/test_vn_article_metadata.py`, `scratch/papervn_worktree/tests/test_vn_backfill_institutions.py`, `scratch/papervn_worktree/docs or handoff artifacts`
- [x] Verification: `Second fixture import inserts zero duplicate links`, `Citation/reference counts do not decrease`, `Existing non-empty citations_by_year survives empty incoming data`, `OA unavailable remains null`, `No live database/API is contacted`, `Read-only SQL checklist includes the audited baseline counts 5 articles, 13 Author_Article, 19 exact-year Institution_Author and 12 distinct institutions as operator verification only`
- [x] Stage gate: Stage tests and operator verification docs after all focused tests pass.

Evidence: Added a repeated backfill fixture test proving no duplicate core rows or links on rerun; existing metadata tests cover non-decreasing metrics, citation history preservation, and OA unknown/null semantics. Added `docs/paper-vn-backfill-operator-checklist.md` with the audited read-only baseline counts.

### Task Card 7: TOOL-07 — Pipeline commands, full verification and handoff

- [x] Objective: Update CLI help and pipeline guidance with --report, checkpoint v2, retry handling, --repair-authorships and --incomplete-authorships. Run compile, unittest, CLI help and fixture dry-run. Update the sprint and handoff with exact commands, staged files, incomplete requirements, known source-unavailable behavior and a safe sequence for 20–50 article validation before any full backfill. Do not commit or push.
- [ ] Files/entrypoints: `scratch/papervn_worktree/tools/run_vn_pipeline.bat`, `scratch/papervn_worktree/tools/vn_journals/backfill_institutions.py`, `scratch/papervn_worktree/.ai/harness/handoff/current.md`, `plans/sprints`
- [x] Verification: `Python compile passes for all changed modules`, `python -m unittest discover -s tests -p "test_vn_*.py" -v passes`, `Backfill --help documents all new flags`, `Fixture dry-run and report generation pass`, `git diff --cached --check passes`, `No .env, dump, cache, pyc, generated production report or checkpoint is staged`, `Handoff explicitly says no full production backfill was run`
- [x] Stage gate: Stage final docs and handoff only after exact verification evidence is recorded; do not commit or push.

Evidence: `python -m compileall tools\vn_journals\backfill_institutions.py tools\vn_journals\paper_vn_affiliations.py tests\test_vn_backfill_institutions.py tests\test_vn_affiliation_import.py` passed; `python -m unittest discover -s tests -p "test_vn_*.py" -v` passed 51 tests; `python tools\vn_journals\backfill_institutions.py --help` documents `--report`, checkpoint v2, `--repair-authorships`, and `--incomplete-authorships`; report generation is covered by temp-file tests; `git diff --cached --check` passed in both the Paper VN worktree and root; staged-file guard found no `.env`, pyc, generated checkpoint/report, dump, cache, or `_ops` paths. Handoff says no full production backfill was run.

## Final Acceptance

- [x] All task cards are checked.
- [x] Required checks pass.
- [x] Handoff explains staged state, residual risks, and next bottleneck if any.
