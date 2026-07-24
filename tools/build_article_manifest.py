"""Build a deterministic, field-balanced Article manifest in production.

The tool never deletes Article rows. By default it runs as a dry-run. Pass
``--apply`` to persist a versioned manifest under the private ``pipeline``
schema after all validations succeed.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env.vercel"
DDL_PATH = REPO_ROOT / "database" / "article_manifest.sql"
TOOLS_DIR = REPO_ROOT / "tools"
ALGORITHM_VERSION = "balanced-fields-v1"
DEFAULT_MANIFEST_NAME = "balanced-20k-v1"
DEFAULT_TARGET_COUNT = 20_000

load_dotenv(ENV_PATH, override=False)

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire  # noqa: E402


SELECTION_SQL = """
CREATE TEMP TABLE temp_article_manifest_selection
ON COMMIT DROP
AS
WITH article_features AS MATERIALIZED (
    SELECT
        a.article_id,
        a.primary_topic,
        t.subject_area_id,
        COALESCE(a.citation_count, 0) AS citation_count,
        COALESCE(a.reference_count, 0) AS reference_count,
        a.publication_year,
        (
            (a.abstract IS NOT NULL AND btrim(a.abstract) <> '')::integer
            + (a.doi IS NOT NULL AND btrim(a.doi) <> '')::integer
            + (a.references IS NOT NULL)::integer
            + (a.semantic_tldr IS NOT NULL AND btrim(a.semantic_tldr) <> '')::integer
        ) AS quality_score,
        (
            COALESCE(a.is_deleted, false) = false
            AND a.title IS NOT NULL
            AND btrim(a.title) <> ''
            AND a.abstract IS NOT NULL
            AND btrim(a.abstract) <> ''
        ) AS eligible
    FROM public."Article" AS a
    LEFT JOIN public."Topic" AS t
        ON t.topic_id = a.primary_topic
),
topic_ranked AS MATERIALIZED (
    SELECT
        f.*,
        row_number() OVER (
            PARTITION BY f.primary_topic
            ORDER BY
                f.quality_score DESC,
                f.citation_count DESC,
                f.reference_count DESC,
                f.publication_year DESC NULLS LAST,
                f.article_id
        ) AS topic_rank
    FROM article_features AS f
    WHERE f.eligible
),
area_ranked AS MATERIALIZED (
    SELECT
        r.*,
        row_number() OVER (
            PARTITION BY COALESCE(r.subject_area_id, -1)
            ORDER BY
                r.topic_rank,
                r.quality_score DESC,
                r.citation_count DESC,
                r.reference_count DESC,
                r.publication_year DESC NULLS LAST,
                r.article_id
        ) AS area_round_rank
    FROM topic_ranked AS r
),
candidates AS (
    SELECT DISTINCT
        f.article_id,
        f.subject_area_id,
        f.primary_topic,
        f.quality_score,
        f.citation_count,
        f.reference_count,
        f.publication_year,
        0 AS priority,
        0::bigint AS area_round_rank,
        'bookmarked'::text AS selection_reason
    FROM article_features AS f
    INNER JOIN public."Project_Article_Bookmark" AS b
        ON b.article_id = f.article_id

    UNION ALL

    SELECT
        r.article_id,
        r.subject_area_id,
        r.primary_topic,
        r.quality_score,
        r.citation_count,
        r.reference_count,
        r.publication_year,
        1 AS priority,
        r.area_round_rank,
        'topic_representative'::text AS selection_reason
    FROM area_ranked AS r
    WHERE r.topic_rank = 1

    UNION ALL

    SELECT
        r.article_id,
        r.subject_area_id,
        r.primary_topic,
        r.quality_score,
        r.citation_count,
        r.reference_count,
        r.publication_year,
        2 AS priority,
        r.area_round_rank,
        'subject_area_balanced'::text AS selection_reason
    FROM area_ranked AS r
),
deduplicated AS (
    SELECT DISTINCT ON (article_id)
        article_id,
        subject_area_id,
        primary_topic,
        quality_score,
        citation_count,
        reference_count,
        publication_year,
        priority,
        area_round_rank,
        selection_reason
    FROM candidates
    ORDER BY article_id, priority
),
ordered AS (
    SELECT
        d.*,
        row_number() OVER (
            ORDER BY
                d.priority,
                CASE WHEN d.priority = 2 THEN d.area_round_rank ELSE 0 END,
                CASE WHEN d.priority = 1
                    THEN COALESCE(d.subject_area_id, -1)
                    ELSE 0
                END,
                CASE WHEN d.priority = 1
                    THEN COALESCE(d.primary_topic, -1)
                    ELSE 0
                END,
                d.quality_score DESC,
                d.citation_count DESC,
                d.reference_count DESC,
                d.publication_year DESC NULLS LAST,
                d.article_id
        ) AS selected_rank
    FROM deduplicated AS d
)
SELECT
    article_id,
    selected_rank::integer,
    selection_reason,
    subject_area_id,
    primary_topic,
    quality_score::smallint,
    citation_count,
    reference_count,
    publication_year
FROM ordered
WHERE selected_rank <= %(target_count)s;
"""


def _database_url() -> str:
    value = os.getenv("VERCEL_DATABASE_URL", "").strip()
    if not value:
        raise ValueError(
            f"VERCEL_DATABASE_URL is required in {ENV_PATH}."
        )
    return value


def _validate_manifest_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", value):
        raise argparse.ArgumentTypeError(
            "Manifest name must be 3-64 lowercase letters, numbers, _ or -."
        )
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a balanced Article manifest without deleting data."
    )
    parser.add_argument(
        "--target",
        type=int,
        default=DEFAULT_TARGET_COUNT,
        help=f"Number of Article rows to select (default: {DEFAULT_TARGET_COUNT}).",
    )
    parser.add_argument(
        "--name",
        type=_validate_manifest_name,
        default=DEFAULT_MANIFEST_NAME,
        help=f"Versioned manifest name (default: {DEFAULT_MANIFEST_NAME}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist and activate the manifest; otherwise only dry-run.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing manifest with the same name.",
    )
    return parser


def _prepare_selection(cursor, target_count: int) -> None:
    cursor.execute(SELECTION_SQL, {"target_count": target_count})
    cursor.execute(
        "CREATE UNIQUE INDEX ON temp_article_manifest_selection (article_id)"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX ON temp_article_manifest_selection (selected_rank)"
    )
    cursor.execute("ANALYZE temp_article_manifest_selection")


def _selection_report(cursor, target_count: int) -> dict:
    cursor.execute(
        """
        SELECT
            count(*)::integer AS selected_count,
            count(DISTINCT article_id)::integer AS distinct_articles,
            count(DISTINCT primary_topic)::integer AS represented_topics,
            count(DISTINCT subject_area_id)::integer AS represented_subject_areas,
            count(*) FILTER (
                WHERE selection_reason = 'bookmarked'
            )::integer AS bookmarked,
            count(*) FILTER (
                WHERE selection_reason = 'topic_representative'
            )::integer AS topic_representatives,
            count(*) FILTER (
                WHERE selection_reason = 'subject_area_balanced'
            )::integer AS balanced_fill,
            count(*) FILTER (WHERE quality_score >= 2)::integer AS quality_ge_2,
            round(avg(citation_count)::numeric, 2) AS average_citations,
            min(publication_year) AS min_year,
            max(publication_year) AS max_year
        FROM temp_article_manifest_selection
        """
    )
    report = dict(cursor.fetchone())
    if report["selected_count"] != target_count:
        raise RuntimeError(
            f"Selection produced {report['selected_count']:,} rows; "
            f"expected exactly {target_count:,}."
        )
    if report["distinct_articles"] != target_count:
        raise RuntimeError("Selection contains duplicate Article IDs.")

    cursor.execute(
        """
        SELECT count(DISTINCT b.article_id)::integer AS missing_bookmarks
        FROM public."Project_Article_Bookmark" AS b
        LEFT JOIN temp_article_manifest_selection AS s
            ON s.article_id = b.article_id
        WHERE s.article_id IS NULL
        """
    )
    missing_bookmarks = cursor.fetchone()["missing_bookmarks"]
    if missing_bookmarks:
        raise RuntimeError(
            f"Selection omitted {missing_bookmarks} bookmarked Article rows."
        )

    cursor.execute(
        """
        SELECT
            min(article_count)::integer AS min_articles,
            max(article_count)::integer AS max_articles,
            round(avg(article_count)::numeric, 2) AS average_articles
        FROM (
            SELECT subject_area_id, count(*) AS article_count
            FROM temp_article_manifest_selection
            WHERE subject_area_id IS NOT NULL
            GROUP BY subject_area_id
        ) AS per_area
        """
    )
    report["per_subject_area"] = dict(cursor.fetchone())
    return report


def _persist_manifest(
    cursor,
    manifest_name: str,
    target_count: int,
    replace: bool,
) -> dict:
    cursor.execute(DDL_PATH.read_text(encoding="utf-8"))
    cursor.execute(
        """
        SELECT 1
        FROM pipeline.article_manifests
        WHERE manifest_name = %s
        """,
        (manifest_name,),
    )
    exists = cursor.fetchone() is not None
    if exists and not replace:
        raise RuntimeError(
            f"Manifest {manifest_name!r} already exists. "
            "Use a new --name or pass --replace."
        )
    if exists:
        cursor.execute(
            """
            DELETE FROM pipeline.article_manifests
            WHERE manifest_name = %s
            """,
            (manifest_name,),
        )

    cursor.execute('SELECT count(*)::bigint AS count FROM public."Article"')
    source_article_count = cursor.fetchone()["count"]
    cursor.execute(
        """
        SELECT md5(string_agg(
            article_id::text,
            ',' ORDER BY selected_rank
        )) AS checksum
        FROM temp_article_manifest_selection
        """
    )
    checksum = cursor.fetchone()["checksum"]

    cursor.execute(
        """
        UPDATE pipeline.article_manifests
        SET is_active = false,
            activated_at = NULL
        WHERE is_active
        """
    )
    cursor.execute(
        """
        INSERT INTO pipeline.article_manifests (
            manifest_name,
            target_count,
            selected_count,
            source_article_count,
            algorithm_version,
            selection_checksum,
            is_active,
            activated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, true, now())
        """,
        (
            manifest_name,
            target_count,
            target_count,
            source_article_count,
            ALGORITHM_VERSION,
            checksum,
        ),
    )
    cursor.execute(
        """
        INSERT INTO pipeline.article_manifest_items (
            manifest_name,
            article_id,
            selected_rank,
            selection_reason,
            subject_area_id,
            primary_topic,
            quality_score,
            citation_count,
            reference_count,
            publication_year
        )
        SELECT
            %s,
            article_id,
            selected_rank,
            selection_reason,
            subject_area_id,
            primary_topic,
            quality_score,
            citation_count,
            reference_count,
            publication_year
        FROM temp_article_manifest_selection
        ORDER BY selected_rank
        """,
        (manifest_name,),
    )
    return {
        "manifest_name": manifest_name,
        "source_article_count": source_article_count,
        "selected_count": target_count,
        "checksum": checksum,
    }


def build_manifest(
    target_count: int,
    manifest_name: str,
    apply: bool,
    replace: bool,
) -> dict:
    if target_count <= 0:
        raise ValueError("--target must be greater than zero.")
    if replace and not apply:
        raise ValueError("--replace is only valid together with --apply.")

    connection = psycopg2.connect(
        _database_url(),
        connect_timeout=20,
        application_name="build_article_manifest",
    )
    try:
        connection.set_session(
            isolation_level="REPEATABLE READ",
            # PostgreSQL rejects CREATE TEMP TABLE AS in a read-only
            # transaction. Dry-run still rolls the entire transaction back,
            # so no persistent object or row survives.
            readonly=False,
            autocommit=False,
        )
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '10min'")
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                ("tool-crawl-scimago:article-manifest",),
            )
            if not cursor.fetchone()["acquired"]:
                raise RuntimeError(
                    "Another server-side manifest build is already running."
                )

            print(
                f"[1] Building {manifest_name!r}: "
                f"target={target_count:,}, algorithm={ALGORITHM_VERSION}"
            )
            _prepare_selection(cursor, target_count)
            report = _selection_report(cursor, target_count)
            print(f"[2] Selection report: {report}")

            if not apply:
                connection.rollback()
                print("[DRY-RUN] Manifest was not persisted; no database data changed.")
                return report

            persisted = _persist_manifest(
                cursor,
                manifest_name,
                target_count,
                replace,
            )
            connection.commit()
            print(
                f"[3] Active manifest saved: {persisted['manifest_name']} "
                f"({persisted['selected_count']:,} Article IDs, "
                f"checksum={persisted['checksum']})"
            )
            return {**report, **persisted}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    args = _build_parser().parse_args()
    try:
        acquire("migrate_local_to_vercel")
        build_manifest(
            target_count=args.target,
            manifest_name=args.name,
            apply=args.apply,
            replace=args.replace,
        )
    except Exception as exc:
        print(f"[ERROR] Manifest build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
