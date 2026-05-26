-- =============================================================
-- Schema: Scientific Journal DB
-- Source: Scimago ETL Import
-- =============================================================

-- ---------------------------------------------------------------
-- publisher
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS publisher (
    publisher_id BIGSERIAL PRIMARY KEY,
    display_name TEXT NOT NULL,
    image_url    TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_publisher_display_name UNIQUE (display_name)
);

-- ---------------------------------------------------------------
-- zone  (country / region lookup)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zone (
    zone_id    BIGSERIAL PRIMARY KEY,
    code       VARCHAR(50),
    name       TEXT        NOT NULL,
    type       VARCHAR(30) NOT NULL,   -- 'country' | 'region'
    source     VARCHAR(30),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_zone_name_type UNIQUE (name, type)
);

-- ---------------------------------------------------------------
-- journal
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal (
    journal_id    BIGSERIAL PRIMARY KEY,
    source_id     TEXT        NOT NULL,
    publisher_id  BIGINT      REFERENCES publisher(publisher_id),
    country_id    BIGINT      REFERENCES zone(zone_id),
    region_id     BIGINT      REFERENCES zone(zone_id),
    display_name  TEXT        NOT NULL,
    type          TEXT,
    is_open_access   BOOLEAN,
    is_oa_diamond    BOOLEAN,
    coverage      TEXT,
    openalex_id         TEXT,
    homepage_url        TEXT,
    works_count         INT,
    cited_by_count      INT,
    openalex_synced_at  TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_journal_source_id UNIQUE (source_id),
    CONSTRAINT uq_journal_openalex_id UNIQUE (openalex_id),
    issn            TEXT
);

-- ---------------------------------------------------------------
-- subject_area  (e.g. Medicine, Computer Science)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subject_area (
    subject_area_id BIGSERIAL PRIMARY KEY,
    display_name    TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_subject_area_name UNIQUE (display_name)
);

-- ---------------------------------------------------------------
-- subject_category  (e.g. Oncology, Artificial Intelligence)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subject_category (
    subject_category_id BIGSERIAL PRIMARY KEY,
    subject_area_id     BIGINT NOT NULL REFERENCES subject_area(subject_area_id),
    display_name        TEXT   NOT NULL,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_subject_category_area_name UNIQUE (subject_area_id, display_name)
);

-- ---------------------------------------------------------------
-- journal_subject_category  (many-to-many)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal_subject_category (
    journal_id          BIGINT NOT NULL REFERENCES journal(journal_id) ON DELETE CASCADE,
    subject_category_id BIGINT NOT NULL REFERENCES subject_category(subject_category_id) ON DELETE CASCADE,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (journal_id, subject_category_id)
);

-- ---------------------------------------------------------------
-- ranking_metric
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ranking_metric (
    metric_id    BIGSERIAL PRIMARY KEY,
    code         VARCHAR(100) NOT NULL,
    display_name TEXT         NOT NULL,
    metric_type  VARCHAR(30)  NOT NULL,   -- INTEGER | SCORE | QUARTILE | TEXT
    description  TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ranking_metric_code UNIQUE (code)
);

-- ---------------------------------------------------------------
-- journal_ranking
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal_ranking (
    journal_ranking_id  BIGSERIAL PRIMARY KEY,
    journal_id          BIGINT NOT NULL REFERENCES journal(journal_id) ON DELETE CASCADE,
    subject_category_id BIGINT REFERENCES subject_category(subject_category_id) ON DELETE CASCADE,
    source              VARCHAR(30) NOT NULL,
    metric_id           BIGINT NOT NULL REFERENCES ranking_metric(metric_id),
    year                INT    NOT NULL,
    value_txt           TEXT,
    value_int           INT,
    value_float         NUMERIC(18, 6),
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Unique index: ranking không gắn với category cụ thể
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_ranking_no_category
    ON journal_ranking (journal_id, source, metric_id, year)
    WHERE subject_category_id IS NULL;

-- Unique index: ranking gắn với category cụ thể (quartile per category)
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_ranking_with_category
    ON journal_ranking (journal_id, subject_category_id, source, metric_id, year)
    WHERE subject_category_id IS NOT NULL;

-- ---------------------------------------------------------------
-- raw_scimago_journal  (staging table)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_scimago_journal (
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
