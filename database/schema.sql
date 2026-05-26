-- =============================================================
-- Schema: Scientific Journal DB (UUID & ENUM Version)
-- Source: Scimago ETL Import & User Update
-- =============================================================

-- Dọn dẹp cấu trúc cũ nếu tồn tại (xóa cả dạng chữ thường cũ và chữ hoa mới để tránh xung đột index/constraint)
DROP TABLE IF EXISTS "Journal_Ranking_Subject_Category" CASCADE;
DROP TABLE IF EXISTS "Journal_Ranking" CASCADE;
DROP TABLE IF EXISTS "Ranking_Metric" CASCADE;
DROP TABLE IF EXISTS "Sub_Topic" CASCADE;
DROP TABLE IF EXISTS "Keyword_Article" CASCADE;
DROP TABLE IF EXISTS "Keyword" CASCADE;
DROP TABLE IF EXISTS "Author_Article" CASCADE;
DROP TABLE IF EXISTS "Author" CASCADE;
DROP TABLE IF EXISTS "Article" CASCADE;
DROP TABLE IF EXISTS "Topic" CASCADE;
DROP TABLE IF EXISTS "Issue" CASCADE;
DROP TABLE IF EXISTS "Volume" CASCADE;
DROP TABLE IF EXISTS "Journal_Subject_Category" CASCADE;
DROP TABLE IF EXISTS "Journal" CASCADE;
DROP TABLE IF EXISTS "Publisher" CASCADE;
DROP TABLE IF EXISTS "Subject_Category" CASCADE;
DROP TABLE IF EXISTS "Subject_Area" CASCADE;
DROP TABLE IF EXISTS "Zone" CASCADE;
DROP TABLE IF EXISTS "user" CASCADE;

-- Drop các bảng chữ thường cũ
DROP TABLE IF EXISTS journal_ranking CASCADE;
DROP TABLE IF EXISTS ranking_metric CASCADE;
DROP TABLE IF EXISTS journal_subject_category CASCADE;
DROP TABLE IF EXISTS journal CASCADE;
DROP TABLE IF EXISTS publisher CASCADE;
DROP TABLE IF EXISTS subject_category CASCADE;
DROP TABLE IF EXISTS subject_area CASCADE;
DROP TABLE IF EXISTS zone CASCADE;
DROP TABLE IF EXISTS raw_scimago_journal CASCADE;

-- Drop các kiểu ENUM cũ nếu có
DROP TYPE IF EXISTS role_account CASCADE;
DROP TYPE IF EXISTS status_account CASCADE;
DROP TYPE IF EXISTS auth_provider CASCADE;
DROP TYPE IF EXISTS type_zone CASCADE;
DROP TYPE IF EXISTS source_zone CASCADE;
DROP TYPE IF EXISTS ranking_source CASCADE;
DROP TYPE IF EXISTS ranking_metric_type CASCADE;

-- 1. Tạo các kiểu ENUM
CREATE TYPE "role_account" AS ENUM (
  'STUDENT',
  'LECTURER',
  'RESEARCHER',
  'ADMINISTRATOR'
);

CREATE TYPE "status_account" AS ENUM (
  'INACTIVE',
  'ACTIVE',
  'BANNED'
);

CREATE TYPE "auth_provider" AS ENUM (
  'LOCAL',
  'GOOGLE'
);

CREATE TYPE "type_zone" AS ENUM (
  'COUNTRY',
  'REGION'
);

CREATE TYPE "source_zone" AS ENUM (
  'ISO',
  'SCIMAGO',
  'OPENALEX',
  'INTERNAL'
);

CREATE TYPE "ranking_source" AS ENUM (
  'SCOPUS',
  'WOS',
  'SCIMAGO'
);

CREATE TYPE "ranking_metric_type" AS ENUM (
  'QUARTILE',
  'SCORE',
  'INTEGER'
);

-- 2. Bảng user
CREATE TABLE "user" (
  "user_id" uuid PRIMARY KEY NOT NULL DEFAULT gen_random_uuid(),
  "email" varchar UNIQUE NOT NULL,
  "password" varchar,
  "type" auth_provider,
  "status" status_account,
  "role" role_account,
  "last_name" varchar,
  "first_name" varchar,
  "url_image" varchar,
  "date_of_birth" date,
  "gender" bool
);

-- 3. Bảng Zone
CREATE TABLE "Zone" (
  "zone_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "code" varchar,
  "name" varchar,
  "type" type_zone,
  "iso_code" varchar,
  "source" source_zone,
  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_zone_name_type UNIQUE ("name", "type")
);

-- 4. Bảng Subject_Area
CREATE TABLE "Subject_Area" (
  "subject_area_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "display_name" varchar,
  "description" varchar,
  CONSTRAINT uq_subject_area_name UNIQUE ("display_name")
);

-- 5. Bảng Subject_Category
CREATE TABLE "Subject_Category" (
  "subject_category_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "subject_area_id" uuid REFERENCES "Subject_Area" ("subject_area_id") DEFERRABLE INITIALLY IMMEDIATE,
  "display_name" varchar,
  "description" varchar,
  CONSTRAINT uq_subject_category_area_name UNIQUE ("subject_area_id", "display_name")
);

-- 6. Bảng Publisher
CREATE TABLE "Publisher" (
  "publisher_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "display_name" varchar,
  "image_url" varchar,
  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_publisher_display_name UNIQUE ("display_name")
);

-- 7. Bảng Journal
CREATE TABLE "Journal" (
  "journal_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "source_id" varchar UNIQUE NOT NULL,
  "publisher_id" uuid REFERENCES "Publisher" ("publisher_id") DEFERRABLE INITIALLY IMMEDIATE,
  "country" uuid REFERENCES "Zone" ("zone_id") DEFERRABLE INITIALLY IMMEDIATE,
  "region" uuid REFERENCES "Zone" ("zone_id") DEFERRABLE INITIALLY IMMEDIATE,
  "display_name" varchar NOT NULL,
  "type" varchar,
  "is_open_access" bool,
  "is_oa_diamond" bool,
  "coverage" varchar,
  "issn" varchar,
  "scope" text,
  
  -- Các cột phục vụ đồng bộ OpenAlex
  "openalex_id" varchar UNIQUE,
  "homepage_url" varchar,
  "works_count" int,
  "cited_by_count" int,
  "openalex_synced_at" timestamp
);

-- 8. Bảng Journal_Subject_Category (Many-to-Many)
CREATE TABLE "Journal_Subject_Category" (
  "journal_id" uuid REFERENCES "Journal" ("journal_id") DEFERRABLE INITIALLY IMMEDIATE,
  "subject_category_id" uuid REFERENCES "Subject_Category" ("subject_category_id") DEFERRABLE INITIALLY IMMEDIATE,
  PRIMARY KEY ("journal_id", "subject_category_id")
);

-- 9. Bảng Volume
CREATE TABLE "Volume" (
  "volume_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "journal_id" uuid REFERENCES "Journal" ("journal_id") DEFERRABLE INITIALLY IMMEDIATE,
  "volume_number" int,
  "publication_year" int
);

-- 10. Bảng Issue
CREATE TABLE "Issue" (
  "issue_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "volume_id" uuid REFERENCES "Volume" ("volume_id") DEFERRABLE INITIALLY IMMEDIATE,
  "issue_number" varchar,
  "publication_year" int
);

-- 11. Bảng Topic
CREATE TABLE "Topic" (
  "topic_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "display_name" varchar,
  "score" double precision
);

-- 12. Bảng Article
CREATE TABLE "Article" (
  "article_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "version" varchar,
  "issue_id" uuid REFERENCES "Issue" ("issue_id") DEFERRABLE INITIALLY IMMEDIATE,
  "title" varchar NOT NULL,
  "abstract" varchar,
  "publication_year" int,
  "doi" varchar,
  "primary_topic" uuid REFERENCES "Topic" ("topic_id") DEFERRABLE INITIALLY IMMEDIATE,
  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP
);

-- 13. Bảng Author
CREATE TABLE "Author" (
  "author_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "orcid" varchar UNIQUE,
  "display_name" varchar,
  "url_image" varchar
);

-- 14. Bảng Author_Article
CREATE TABLE "Author_Article" (
  "author_id" uuid REFERENCES "Author" ("author_id") DEFERRABLE INITIALLY IMMEDIATE,
  "article_id" uuid REFERENCES "Article" ("article_id") DEFERRABLE INITIALLY IMMEDIATE,
  PRIMARY KEY ("author_id", "article_id")
);

-- 15. Bảng Keyword
CREATE TABLE "Keyword" (
  "keyword_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "display_name" varchar
);

-- 16. Bảng Keyword_Article
CREATE TABLE "Keyword_Article" (
  "keyword_id" uuid REFERENCES "Keyword" ("keyword_id") DEFERRABLE INITIALLY IMMEDIATE,
  "article_id" uuid REFERENCES "Article" ("article_id") DEFERRABLE INITIALLY IMMEDIATE,
  "score" double precision,
  PRIMARY KEY ("keyword_id", "article_id")
);

-- 17. Bảng Sub_Topic
CREATE TABLE "Sub_Topic" (
  "article_id" uuid REFERENCES "Article" ("article_id") DEFERRABLE INITIALLY IMMEDIATE,
  "topic_id" uuid REFERENCES "Topic" ("topic_id") DEFERRABLE INITIALLY IMMEDIATE,
  PRIMARY KEY ("article_id", "topic_id")
);

-- 18. Bảng Ranking_Metric
CREATE TABLE "Ranking_Metric" (
  "metric_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "code" varchar UNIQUE,
  "display_name" varchar,
  "metric_type" ranking_metric_type,
  "description" varchar
);

-- 19. Bảng Journal_Ranking
CREATE TABLE "Journal_Ranking" (
  "journal_ranking_id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "journal_id" uuid NOT NULL REFERENCES "Journal" ("journal_id") DEFERRABLE INITIALLY IMMEDIATE,
  "subject_category_id" uuid REFERENCES "Subject_Category" ("subject_category_id") DEFERRABLE INITIALLY IMMEDIATE,
  "source" ranking_source NOT NULL DEFAULT 'SCIMAGO',
  "metric_id" uuid NOT NULL REFERENCES "Ranking_Metric" ("metric_id") DEFERRABLE INITIALLY IMMEDIATE,
  "year" int NOT NULL,
  "value_txt" varchar,
  "value_int" int,
  "value_float" double precision,
  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP
);

-- 20. Bảng Journal_Ranking_Subject_Category
CREATE TABLE "Journal_Ranking_Subject_Category" (
  "journal_ranking_id" uuid REFERENCES "Journal_Ranking" ("journal_ranking_id") DEFERRABLE INITIALLY IMMEDIATE,
  "subject_category_id" uuid REFERENCES "Subject_Category" ("subject_category_id") DEFERRABLE INITIALLY IMMEDIATE
);

-- 21. Bảng staging raw_scimago_journal
CREATE TABLE raw_scimago_journal (
    raw_id                  BIGSERIAL PRIMARY KEY,
    import_batch_id         UUID      NOT NULL,
    rank_txt                TEXT,
    source_id               TEXT,
    title                   TEXT,
    type                    TEXT,
    issn                    TEXT,
    publisher               TEXT,
    open_access             TEXT,
    open_access_diamond     TEXT,
    sjr                     TEXT,
    h_index                 TEXT,
    total_docs_current_year TEXT,
    total_docs_3years       TEXT,
    total_refs              TEXT,
    total_cites_3years      TEXT,
    citable_docs_3years     TEXT,
    cites_doc_2years        TEXT,
    ref_doc                 TEXT,
    country                 TEXT,
    region                  TEXT,
    categories              TEXT,
    areas                   TEXT,
    raw_json                JSONB,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Comments on columns
COMMENT ON COLUMN "user"."password" IS 'Mật khẩu đã mã hóa băm (hashing)';
COMMENT ON COLUMN "user"."type" IS 'Phương thức đăng nhập';
COMMENT ON COLUMN "user"."status" IS 'Trạng thái tài khoản';
COMMENT ON COLUMN "user"."gender" IS '0: Nữ, 1: Nam';
COMMENT ON COLUMN "Zone"."code" IS 'Ví dụ: VN, ASIA, EU, GLOBAL';
COMMENT ON COLUMN "Zone"."name" IS 'Tên quốc gia hoặc khu vực';
COMMENT ON COLUMN "Subject_Area"."display_name" IS 'Lĩnh vực lớn (Ví dụ: Medicine, Social Sciences từ Scimago)';
COMMENT ON COLUMN "Subject_Category"."subject_area_id" IS 'Thuộc về lĩnh vực lớn nào';
COMMENT ON COLUMN "Subject_Category"."display_name" IS 'Chuyên ngành hẹp (Ví dụ: Oncology, Cultural Studies)';
COMMENT ON COLUMN "Journal"."source_id" IS 'ID gốc từ Scimago (Sourceid) hoặc OpenAlex để đồng bộ';
COMMENT ON COLUMN "Journal"."country" IS 'Quốc gia/khu vực quản lý tạp chí';
COMMENT ON COLUMN "Journal"."region" IS 'Quốc gia/khu vực quản lý tạp chí';
COMMENT ON COLUMN "Journal"."display_name" IS 'Tên tạp chí';
COMMENT ON COLUMN "Journal"."type" IS 'journal, book series, conference and proceedings, trade journal';
COMMENT ON COLUMN "Journal"."is_open_access" IS 'Trường Open Access (Yes/No) từ Scimago 2025';
COMMENT ON COLUMN "Journal"."is_oa_diamond" IS 'Trường Open Access Diamond (Yes/No) từ Scimago 2025';
COMMENT ON COLUMN "Journal"."coverage" IS 'Thời gian bao phủ dữ liệu (Ví dụ: 1950-2026)';
COMMENT ON COLUMN "Article"."version" IS 'Phiên bản bài báo';
COMMENT ON COLUMN "Author"."orcid" IS 'Mã định danh quốc tế của tác giả';
COMMENT ON COLUMN "Keyword_Article"."score" IS 'Trọng số của từ khóa đối với riêng bài báo này';
COMMENT ON COLUMN "Ranking_Metric"."code" IS 'Mã định danh chỉ số để map code hệ thống';
COMMENT ON COLUMN "Ranking_Metric"."display_name" IS 'Tên hiển thị từ file excel';
COMMENT ON TABLE "Journal_Ranking" IS 'Bảng lưu trữ động toàn bộ chỉ số xếp hạng theo năm của Tạp chí';
COMMENT ON COLUMN "Journal_Ranking"."subject_category_id" IS 'Null nếu là chỉ số chung của Tạp chí (như H-index, Total Docs). Có giá trị nếu là Rank hoặc Quartile riêng của Tạp chí trong Chuyên ngành đó';
COMMENT ON COLUMN "Journal_Ranking"."year" IS 'Ví dụ: 2025';
COMMENT ON COLUMN "Journal_Ranking"."value_txt" IS 'Lưu giá trị nếu metric_type = QUARTILE (Ví dụ: Q1)';
COMMENT ON COLUMN "Journal_Ranking"."value_int" IS 'Lưu giá trị nếu metric_type = INTEGER (Ví dụ: 236, 4331)';
COMMENT ON COLUMN "Journal_Ranking"."value_float" IS 'Lưu giá trị nếu metric_type = SCORE (Ví dụ: 104.065, 46.34)';

-- Unique indexes cho Journal_Ranking (partial indexes) chống trùng lặp xếp hạng
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_ranking_no_category
    ON "Journal_Ranking" (journal_id, source, metric_id, year)
    WHERE subject_category_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_ranking_with_category
    ON "Journal_Ranking" (journal_id, subject_category_id, source, metric_id, year)
    WHERE subject_category_id IS NOT NULL;

-- Index cho raw_scimago_journal để tối ưu hóa truy vấn export report tránh bị treo
CREATE INDEX IF NOT EXISTS idx_raw_scimago_journal_source_id
    ON raw_scimago_journal (source_id);

