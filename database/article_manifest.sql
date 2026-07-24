CREATE SCHEMA IF NOT EXISTS pipeline;
REVOKE ALL ON SCHEMA pipeline FROM PUBLIC;

CREATE TABLE IF NOT EXISTS pipeline.article_manifests (
    manifest_name text PRIMARY KEY,
    target_count integer NOT NULL CHECK (target_count > 0),
    selected_count integer NOT NULL CHECK (selected_count > 0),
    source_article_count bigint NOT NULL CHECK (source_article_count >= selected_count),
    algorithm_version text NOT NULL,
    selection_checksum text NOT NULL,
    is_active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS article_manifests_one_active_idx
    ON pipeline.article_manifests (is_active)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS pipeline.article_manifest_items (
    manifest_name text NOT NULL
        REFERENCES pipeline.article_manifests (manifest_name)
        ON DELETE CASCADE,
    article_id bigint NOT NULL
        REFERENCES public."Article" (article_id)
        ON DELETE RESTRICT,
    selected_rank integer NOT NULL CHECK (selected_rank > 0),
    selection_reason text NOT NULL CHECK (
        selection_reason IN (
            'bookmarked',
            'topic_representative',
            'subject_area_balanced'
        )
    ),
    subject_area_id bigint,
    primary_topic bigint,
    quality_score smallint NOT NULL,
    citation_count integer NOT NULL,
    reference_count integer NOT NULL,
    publication_year integer,
    PRIMARY KEY (manifest_name, article_id),
    UNIQUE (manifest_name, selected_rank)
);

CREATE INDEX IF NOT EXISTS article_manifest_items_article_idx
    ON pipeline.article_manifest_items (article_id);

CREATE TABLE IF NOT EXISTS pipeline.article_prune_runs (
    run_name text PRIMARY KEY,
    manifest_name text NOT NULL
        REFERENCES pipeline.article_manifests (manifest_name)
        ON DELETE RESTRICT,
    manifest_checksum text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('running', 'paused', 'failed', 'completed')
    ),
    current_stage text,
    deleted_rows jsonb NOT NULL DEFAULT '{}'::jsonb,
    initial_report jsonb NOT NULL,
    backup_path text NOT NULL,
    batch_size integer NOT NULL CHECK (batch_size > 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    last_error text
);

REVOKE ALL ON pipeline.article_manifests FROM PUBLIC;
REVOKE ALL ON pipeline.article_manifest_items FROM PUBLIC;
REVOKE ALL ON pipeline.article_prune_runs FROM PUBLIC;
