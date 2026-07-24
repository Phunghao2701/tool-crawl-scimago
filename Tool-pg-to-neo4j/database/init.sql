-- PostgreSQL Database Schema Initialization

-- Drop tables if they exist to allow clean re-runs
DROP TABLE IF EXISTS "Subject_Category_Project" CASCADE;
DROP TABLE IF EXISTS "Project_Journal" CASCADE;
DROP TABLE IF EXISTS "Project_Keyword" CASCADE;
DROP TABLE IF EXISTS "Project" CASCADE;
DROP TABLE IF EXISTS "Password_Reset_Token" CASCADE;
DROP TABLE IF EXISTS "user" CASCADE;
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
DROP TABLE IF EXISTS "Journal_Ranking" CASCADE;
DROP TABLE IF EXISTS "Ranking_Metric" CASCADE;
DROP TABLE IF EXISTS "Journal_Ranking_Subject_Category" CASCADE;
DROP TABLE IF EXISTS "Subject_Category" CASCADE;
DROP TABLE IF EXISTS "Subject_Area" CASCADE;
DROP TABLE IF EXISTS "Journal" CASCADE;
DROP TABLE IF EXISTS "Publisher" CASCADE;
DROP TABLE IF EXISTS "Zone" CASCADE;

-- Drop Enums
DROP TYPE IF EXISTS role_account CASCADE;
DROP TYPE IF EXISTS status_account CASCADE;
DROP TYPE IF EXISTS auth_provider CASCADE;
DROP TYPE IF EXISTS type_zone CASCADE;
DROP TYPE IF EXISTS source_zone CASCADE;
DROP TYPE IF EXISTS ranking_source CASCADE;
DROP TYPE IF EXISTS ranking_metric_type CASCADE;


-- ==========================================
-- 1. ENUMS
-- ==========================================
CREATE TYPE role_account AS ENUM ('STUDENT', 'LECTURER', 'RESEARCHER', 'ADMINISTRATOR');
CREATE TYPE status_account AS ENUM ('INACTIVE', 'ACTIVE', 'BANNED');
CREATE TYPE auth_provider AS ENUM ('LOCAL', 'GOOGLE');
CREATE TYPE type_zone AS ENUM ('COUNTRY', 'REGION');
CREATE TYPE source_zone AS ENUM ('ISO', 'SCIMAGO', 'OPENALEX', 'INTERNAL');
CREATE TYPE ranking_source AS ENUM ('SCOPUS', 'WOS', 'SCIMAGO');
CREATE TYPE ranking_metric_type AS ENUM ('QUARTILE', 'SCORE', 'INTEGER');

-- ==========================================
-- 2. ACCOUNTS & USERS
-- ==========================================
CREATE TABLE "user" (
  user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email varchar NOT NULL UNIQUE,
  password varchar,
  type auth_provider,
  status status_account,
  role role_account,
  last_name varchar,
  first_name varchar,
  url_image varchar,
  date_of_birth date,
  gender boolean
);

CREATE TABLE "Password_Reset_Token" (
  token_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES "user"(user_id) ON DELETE CASCADE,
  token_hash varchar(255) NOT NULL,
  expires_at timestamp NOT NULL,
  used_at timestamp,
  created_at timestamp DEFAULT CURRENT_TIMESTAMP
);

-- ==========================================
-- 3. ZONES & CATEGORIES
-- ==========================================
CREATE TABLE "Zone" (
  zone_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code varchar,
  name varchar,
  type type_zone,
  iso_code varchar,
  source source_zone,
  created_at timestamp DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE "Subject_Area" (
  subject_area_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name varchar,
  description varchar,
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Subject_Category" (
  subject_category_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_area_id bigint REFERENCES "Subject_Area"(subject_area_id),
  display_name varchar,
  description varchar,
  is_deleted boolean DEFAULT false
);

-- ==========================================
-- 4. PUBLISHERS & JOURNALS
-- ==========================================
CREATE TABLE "Publisher" (
  publisher_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name varchar,
  image_url varchar,
  created_at timestamp DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE "Journal" (
  journal_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id varchar,
  publisher_id bigint REFERENCES "Publisher"(publisher_id),
  country bigint REFERENCES "Zone"(zone_id),
  region bigint REFERENCES "Zone"(zone_id),
  display_name varchar,
  type varchar,
  is_open_access boolean,
  is_oa_diamond boolean,
  coverage varchar,
  issn varchar,
  scope_detail text,
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Journal_Subject_Category" (
  journal_id bigint REFERENCES "Journal"(journal_id) ON DELETE CASCADE,
  subject_category_id bigint REFERENCES "Subject_Category"(subject_category_id) ON DELETE CASCADE,
  PRIMARY KEY (journal_id, subject_category_id)
);

-- ==========================================
-- 5. RANKING & METRICS
-- ==========================================
CREATE TABLE "Ranking_Metric" (
  metric_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code varchar UNIQUE,
  display_name varchar,
  metric_type ranking_metric_type,
  description varchar
);

CREATE TABLE "Journal_Ranking" (
  journal_ranking_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  journal_id bigint NOT NULL REFERENCES "Journal"(journal_id),
  subject_category_id bigint REFERENCES "Subject_Category"(subject_category_id),
  source ranking_source NOT NULL DEFAULT 'SCIMAGO',
  metric_id bigint NOT NULL REFERENCES "Ranking_Metric"(metric_id),
  year int NOT NULL,
  value_txt varchar,
  value_int int,
  value_float double precision,
  created_at timestamp DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE "Journal_Ranking_Subject_Category" (
  journal_ranking_id bigint REFERENCES "Journal_Ranking"(journal_ranking_id) ON DELETE CASCADE,
  subject_category_id bigint REFERENCES "Subject_Category"(subject_category_id) ON DELETE CASCADE,
  PRIMARY KEY (journal_ranking_id, subject_category_id)
);

-- ==========================================
-- 6. ARTICLES & AUTHORS
-- ==========================================
CREATE TABLE "Volume" (
  volume_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  journal_id bigint REFERENCES "Journal"(journal_id),
  volume_number int,
  publication_year int,
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Issue" (
  issue_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  volume_id bigint REFERENCES "Volume"(volume_id),
  issue_number varchar,
  publication_year int,
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Topic" (
  topic_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name varchar,
  score double precision,
  subject_area_id bigint REFERENCES "Subject_Area"(subject_area_id),
  subject_category_id bigint REFERENCES "Subject_Category"(subject_category_id),
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Article" (
  article_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version varchar,
  issue_id bigint REFERENCES "Issue"(issue_id),
  title varchar NOT NULL,
  abstract text,
  publication_year int,
  doi varchar,
  primary_topic bigint REFERENCES "Topic"(topic_id),
  created_at timestamp DEFAULT CURRENT_TIMESTAMP,
  is_deleted boolean DEFAULT false,
  cited_by_count bigint,
  final_references jsonb
);

CREATE TABLE "Author" (
  author_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  orcid varchar UNIQUE,
  display_name varchar,
  url_image varchar,
  openalex_id varchar,
  works_count bigint,
  cited_by_count bigint,
  h_index bigint,
  i10_index bigint,
  is_deleted boolean DEFAULT false
);

CREATE TABLE "Author_Article" (
  author_id bigint REFERENCES "Author"(author_id) ON DELETE CASCADE,
  article_id bigint REFERENCES "Article"(article_id) ON DELETE CASCADE,
  PRIMARY KEY (author_id, article_id)
);

CREATE TABLE "Keyword" (
  keyword_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  display_name varchar
);

CREATE TABLE "Keyword_Article" (
  keyword_id bigint REFERENCES "Keyword"(keyword_id) ON DELETE CASCADE,
  article_id bigint REFERENCES "Article"(article_id) ON DELETE CASCADE,
  score double precision,
  PRIMARY KEY (keyword_id, article_id)
);

CREATE TABLE "Sub_Topic" (
  article_id bigint REFERENCES "Article"(article_id) ON DELETE CASCADE,
  topic_id bigint REFERENCES "Topic"(topic_id) ON DELETE CASCADE,
  PRIMARY KEY (article_id, topic_id)
);

-- ==========================================
-- 7. PROJECTS
-- ==========================================
CREATE TABLE "Project" (
  project_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid REFERENCES "user"(user_id),
  subject_area bigint REFERENCES "Subject_Area"(subject_area_id),
  title varchar,
  created_at timestamp DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE "Project_Keyword" (
  project_id bigint REFERENCES "Project"(project_id) ON DELETE CASCADE,
  keyword_id bigint REFERENCES "Keyword"(keyword_id) ON DELETE CASCADE,
  PRIMARY KEY (project_id, keyword_id)
);

CREATE TABLE "Subject_Category_Project" (
  project_id bigint REFERENCES "Project"(project_id) ON DELETE CASCADE,
  subject_category_id bigint REFERENCES "Subject_Category"(subject_category_id) ON DELETE CASCADE,
  PRIMARY KEY (project_id, subject_category_id)
);

CREATE TABLE "Project_Journal" (
  project_id bigint REFERENCES "Project"(project_id) ON DELETE CASCADE,
  journal_id bigint REFERENCES "Journal"(journal_id) ON DELETE CASCADE,
  PRIMARY KEY (project_id, journal_id)
);
