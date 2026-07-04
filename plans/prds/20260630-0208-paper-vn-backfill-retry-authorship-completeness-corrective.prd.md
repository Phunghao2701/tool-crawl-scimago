---
title: "Paper VN Backfill Retry and Authorship Completeness Corrective"
kind: "prd"
created_at: "2026-06-29T19:08:53.522Z"
source: "repo-harness-mcp"
---
# Paper VN Backfill Retry and Authorship Completeness Corrective

> **Status**: Draft

## Idea

Sửa các lỗi runtime còn lại trong backfill affiliation sau sprint classification/recovery: retry queue hiện được thêm vào memory nhưng không được persist khi gặp RETRYABLE và cũng không được consume khi resume; queue không được làm sạch khi trạng thái thay đổi; --repair-authorships không xử lý trường hợp Author đã tồn tại nhưng chưa có Author_Article cho article; dry-run repair có thể bị gắn nhãn AUTHOR_UNRESOLVED dù repair hợp lệ; --incomplete-authorships chọn đúng article nhưng vẫn đánh giá SUCCESS bằng article_has_exact_year_affiliation nên có thể báo thành công giả khi chỉ một author có affiliation.

## Problem

Current implementation can lose retryable state at process exit, never retries queued article IDs on resume, and leaves stale IDs in queues after status transitions. Existing-but-unlinked authors are not repaired because repair helper is only called when resolve_author_id returns None. In incomplete-authorship mode, article-level exact-year affiliation is insufficient to determine author-level completeness, so partial articles such as Article 4 can be reported SUCCESS while some authors remain incomplete.

## Users

- Người vận hành Paper VN backfill
- Nhóm dữ liệu ScienceJournalTrendingVN
- Nhóm kiểm thử crawler/importer

## Goals

- Persist retryable outcomes before process exits without advancing last_scanned_article_id
- Consume retryable_article_ids first on --resume, then continue forward scanning
- Remove article IDs from all old queues before assigning a new terminal/retry status
- Repair Author_Article when an identified Author already exists but is not linked to the article
- Keep authorship repair opt-in and safe by OpenAlex identity
- Make dry-run distinguish PLANNED_AUTHOR_REPAIR from genuinely unresolved identity
- Measure author-level completeness before and after processing in incomplete-authorship mode
- Prevent SUCCESS when any linked article author still lacks exact-year Institution_Author unless the source-unavailable state is explicitly represented
- Add focused regression tests for retry persistence/consumption, queue transitions, existing-author link repair, dry-run repair status and partial-author completeness
- Synchronize sprint closeout, root/worktree handoff and checks evidence

## Non-goals

- Không sửa FE hoặc BE
- Không thay đổi database schema hoặc tạo migration
- Không chạy full production backfill
- Không tự merge authors chỉ theo tên
- Không thay đổi importer core ngoài phần cần thiết cho repair/backfill
- Không commit hoặc push

## Acceptance Criteria

- [ ] RETRYABLE outcome writes checkpoint v2 with previous last_scanned_article_id and retryable_article_ids containing the failed article
- [ ] --resume processes durable retryable IDs before normal article_id scan
- [ ] Successful retry removes the ID from retryable/failed/unavailable queues
- [ ] Status transitions never leave stale IDs in prior queues
- [ ] --repair-authorships creates missing Author_Article for an existing Author identified safely by OpenAlex ID
- [ ] Dry-run repair reports planned repair without classifying the same author as unresolved
- [ ] Incomplete-authorship mode reports author completeness counts before and after
- [ ] Article with one complete author and two incomplete authors is not SUCCESS until all repairable authors are complete; source-unavailable authors are explicitly represented
- [ ] All focused and full test_vn_* tests pass without production DB/live API access
- [ ] Handoff and checks files contain exact verification evidence and no full production backfill claim

## Workflow Contract

- PRD is the source of product intent.
- Sprint must be generated as ordered checklist task cards.
- Codex execution must happen through a host-native `/goal` prompt or local Codex session, not through remote MCP execution.

## Handoff Notes

Work only inside existing worktree scratch/papervn_worktree on branch hao/feature/paper-vn. Do not checkout at repository root or create/remove worktrees. Preserve unrelated dirty changes. Current sprint reported 51 tests passing, but tests do not cover real retry queue consumption, existing Author without Author_Article, or Article-4-style partial completeness. Keep this corrective sprint narrow and do not rework the already-correct main affiliation importer.
