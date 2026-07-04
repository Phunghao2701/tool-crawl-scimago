---
title: "Paper VN Backfill Outcome Classification and Recovery"
kind: "prd"
created_at: "2026-06-29T18:52:46.946Z"
source: "repo-harness-mcp"
---
# Paper VN Backfill Outcome Classification and Recovery

> **Status**: Draft

## Idea

Hoàn thiện legacy affiliation backfill sau khi smoke test cho thấy importer mới ghi affiliation đúng, nhưng checkpoint hiện chỉ lưu last_completed_article_id và gộp mọi bài chưa có affiliation vào một counter. Cần phân loại kết quả theo article, xuất report chi tiết, tách source-unavailable khỏi lỗi retryable, hỗ trợ repair authorship có kiểm soát cho article thiếu Author_Article, bổ sung chế độ tìm authorship chưa đầy đủ, và xác nhận idempotency trước khi chạy batch lớn.

## Problem

Read-only audit cho thấy checkpoint đã đi tới article_id 276 nhưng còn 18 article chưa có exact-year affiliation. Phần lớn là work cũ có thể không có institution từ OpenAlex; một article không có Author_Article; Article 4 chỉ có affiliation cho 1/3 authors. Tool hiện không ghi article ID, reason, authorship count hay retry status, nên không thể phân biệt dữ liệu nguồn không có với lỗi thật. Resume sẽ bỏ qua các article đã scan, còn --only-missing chỉ kiểm tra mức article và không phát hiện authorship chưa đầy đủ.

## Users

- Người vận hành Paper VN crawler/backfill
- Nhóm dữ liệu và backend ScienceJournalTrendingVN
- Người kiểm tra chất lượng affiliation trước khi chạy full backfill

## Goals

- Thêm outcome model theo từng article với status và reason rõ ràng
- Xuất report JSON/JSONL hoặc CSV không chứa secret cho mọi article đã scan
- Đổi checkpoint semantic sang last_scanned_article_id và lưu retryable/unavailable/failed IDs tách biệt
- Không retry vô hạn các work mà OpenAlex xác nhận không có authorship institution
- Không bỏ qua transient failures hoặc unresolved/failed records khỏi retry queue
- Thêm repair authorship có kiểm soát và chỉ chạy khi bật flag rõ ràng
- Hỗ trợ kiểm tra incomplete authorships ngoài article-level missing affiliation
- Giữ backfill idempotent, additive và không xóa dữ liệu
- Chứng minh importer/backfill idempotency với fixture và hướng dẫn production verification
- Cập nhật handoff với exact commands và SQL read-only

## Non-goals

- Không sửa FE hoặc BE
- Không thay đổi schema hoặc tạo migration
- Không tự động chạy full production backfill
- Không tự merge Author chỉ dựa trên display_name
- Không cố tạo affiliation giả khi OpenAlex không có dữ liệu
- Không commit hoặc push

## Acceptance Criteria

- [ ] Mỗi article được gán một status trong SUCCESS, UNAVAILABLE_FROM_SOURCE, RETRYABLE, AUTHOR_UNRESOLVED, ARTICLE_HAS_NO_AUTHORS, FAILED hoặc tương đương có tài liệu rõ
- [ ] Report chứa article_id, publication_year, identifiers, authorship_count, authorships_with_institutions, institutions_found, links_inserted, unresolved authors và reason
- [ ] Checkpoint version mới lưu last_scanned_article_id, cumulative stats và các queue/list cần thiết; old checkpoint được reject hoặc migrate rõ ràng
- [ ] Transient 429/5xx/timeout không bị đánh dấu complete và có thể retry
- [ ] Work có authorships nhưng không có institutions được đánh dấu unavailable, không retry vô hạn
- [ ] Flag --repair-authorships có dry-run và chỉ khi bật mới được upsert Author/Author_Article bị thiếu
- [ ] Có mode --incomplete-authorships hoặc tương đương để tìm Author_Article chưa có exact-year Institution_Author
- [ ] Article-level --only-missing tiếp tục hoạt động cho Paper VN scope
- [ ] Unit tests bao phủ classification, checkpoint v2, retry queue, unavailable source, authorship repair, incomplete authorships và idempotency
- [ ] Compile, unittest, CLI help và fixture dry-run pass; không gọi production DB/live API trong tests

## Workflow Contract

- PRD is the source of product intent.
- Sprint must be generated as ordered checklist task cards.
- Codex execution must happen through a host-native `/goal` prompt or local Codex session, not through remote MCP execution.

## Handoff Notes

Báo cáo hiện tại xác nhận importer mới ghi affiliation đúng cho 5 article, 4/5 thuộc Paper VN, multi-affiliation đúng, không duplicate, citations_by_year đúng object. Có 18 article <= checkpoint 276 chưa exact-year affiliation; 17 có Author_Article, 24 authors có OpenAlex ID nhưng 0 exact-year institution; article 270 không có Author_Article; Article 4 chỉ 1/3 authors có affiliation. Nhiều work rất cũ nên source-unavailable là outcome hợp lệ. Work only inside existing worktree scratch/papervn_worktree on branch hao/feature/paper-vn; do not checkout branch at root.
