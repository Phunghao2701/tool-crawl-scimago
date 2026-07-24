"""Report what an in-place prune to the active Article manifest would remove.

This tool is deliberately report-only. It creates temporary closure tables,
prints verified keep/delete counts, and rolls the transaction back. It
contains no production DELETE or TRUNCATE operation.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env.vercel"
TOOLS_DIR = REPO_ROOT / "tools"

load_dotenv(ENV_PATH, override=False)

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire  # noqa: E402


REPORT_TABLES = (
    "Article",
    "Author_Article",
    "Keyword_Article",
    "Sub_Topic",
    "Author",
    "Institution_Author",
    "Institution",
    "Keyword",
    "Issue",
    "Volume",
    "Journal",
    "Journal_Subject_Category",
    "Journal_Ranking",
    "Journal_Ranking_Subject_Category",
)

KEEP_TABLES = {
    "Article": "temp_keep_article",
    "Author_Article": "temp_keep_article",
    "Keyword_Article": "temp_keep_article",
    "Sub_Topic": "temp_keep_article",
    "Author": "temp_keep_author",
    "Institution_Author": "temp_keep_author",
    "Institution": "temp_keep_institution",
    "Keyword": "temp_keep_keyword",
    "Issue": "temp_keep_issue",
    "Volume": "temp_keep_volume",
    "Journal": "temp_keep_journal",
    "Journal_Subject_Category": "temp_keep_journal",
    "Journal_Ranking": "temp_keep_journal",
    "Journal_Ranking_Subject_Category": "temp_keep_ranking",
}

JOIN_COLUMNS = {
    "Article": ("article_id", "article_id"),
    "Author_Article": ("article_id", "article_id"),
    "Keyword_Article": ("article_id", "article_id"),
    "Sub_Topic": ("article_id", "article_id"),
    "Author": ("author_id", "author_id"),
    "Institution_Author": ("author_id", "author_id"),
    "Institution": ("institution_id", "institution_id"),
    "Keyword": ("keyword_id", "keyword_id"),
    "Issue": ("issue_id", "issue_id"),
    "Volume": ("volume_id", "volume_id"),
    "Journal": ("journal_id", "journal_id"),
    "Journal_Subject_Category": ("journal_id", "journal_id"),
    "Journal_Ranking": ("journal_id", "journal_id"),
    "Journal_Ranking_Subject_Category": (
        "journal_ranking_id",
        "journal_ranking_id",
    ),
}


def _database_url() -> str:
    value = os.getenv("VERCEL_DATABASE_URL", "").strip()
    if not value:
        raise ValueError(f"VERCEL_DATABASE_URL is required in {ENV_PATH}.")
    return value


def _active_manifest(cursor) -> dict:
    cursor.execute(
        """
        SELECT manifest_name, selected_count, source_article_count,
               algorithm_version, selection_checksum
        FROM pipeline.article_manifests
        WHERE is_active
        """
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("No active Article manifest exists.")
    return dict(row)


def _create_closure(cursor, manifest_name: str) -> None:
    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_article
        ON COMMIT PRESERVE ROWS
        AS
        SELECT article_id
        FROM pipeline.article_manifest_items
        WHERE manifest_name = %s
        """,
        (manifest_name,),
    )
    cursor.execute(
        "CREATE UNIQUE INDEX ON temp_keep_article (article_id)"
    )

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_author
        ON COMMIT PRESERVE ROWS
        AS
        SELECT DISTINCT aa.author_id
        FROM public."Author_Article" AS aa
        INNER JOIN temp_keep_article AS a USING (article_id)
        """
    )
    cursor.execute("CREATE UNIQUE INDEX ON temp_keep_author (author_id)")

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_keyword
        ON COMMIT PRESERVE ROWS
        AS
        SELECT keyword_id
        FROM (
            SELECT DISTINCT ka.keyword_id
            FROM public."Keyword_Article" AS ka
            INNER JOIN temp_keep_article AS a USING (article_id)
            UNION
            SELECT DISTINCT pk.keyword_id
            FROM public."Project_Keyword" AS pk
        ) AS kept
        """
    )
    cursor.execute("CREATE UNIQUE INDEX ON temp_keep_keyword (keyword_id)")

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_issue
        ON COMMIT PRESERVE ROWS
        AS
        SELECT DISTINCT article.issue_id
        FROM public."Article" AS article
        INNER JOIN temp_keep_article AS kept USING (article_id)
        WHERE article.issue_id IS NOT NULL
        """
    )
    cursor.execute("CREATE UNIQUE INDEX ON temp_keep_issue (issue_id)")

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_volume
        ON COMMIT PRESERVE ROWS
        AS
        SELECT DISTINCT issue.volume_id
        FROM public."Issue" AS issue
        INNER JOIN temp_keep_issue AS kept USING (issue_id)
        WHERE issue.volume_id IS NOT NULL
        """
    )
    cursor.execute("CREATE UNIQUE INDEX ON temp_keep_volume (volume_id)")

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_journal
        ON COMMIT PRESERVE ROWS
        AS
        SELECT journal_id
        FROM (
            SELECT DISTINCT volume.journal_id
            FROM public."Volume" AS volume
            INNER JOIN temp_keep_volume AS kept USING (volume_id)
            WHERE volume.journal_id IS NOT NULL
            UNION
            SELECT DISTINCT pj.journal_id
            FROM public."Project_Journal" AS pj
        ) AS kept
        """
    )
    cursor.execute("CREATE UNIQUE INDEX ON temp_keep_journal (journal_id)")

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_ranking
        ON COMMIT PRESERVE ROWS
        AS
        SELECT ranking.journal_ranking_id
        FROM public."Journal_Ranking" AS ranking
        INNER JOIN temp_keep_journal AS kept USING (journal_id)
        """
    )
    cursor.execute(
        "CREATE UNIQUE INDEX ON temp_keep_ranking (journal_ranking_id)"
    )

    cursor.execute(
        """
        CREATE TEMP TABLE temp_keep_institution
        ON COMMIT PRESERVE ROWS
        AS
        SELECT DISTINCT ia.institution_id
        FROM public."Institution_Author" AS ia
        INNER JOIN temp_keep_author AS kept USING (author_id)
        """
    )
    cursor.execute(
        "CREATE UNIQUE INDEX ON temp_keep_institution (institution_id)"
    )

    for table in (
        "temp_keep_article",
        "temp_keep_author",
        "temp_keep_keyword",
        "temp_keep_issue",
        "temp_keep_volume",
        "temp_keep_journal",
        "temp_keep_ranking",
        "temp_keep_institution",
    ):
        cursor.execute(
            sql.SQL("ANALYZE {}").format(sql.Identifier(table))
        )


def _table_count(cursor, table_name: str) -> int:
    cursor.execute(
        sql.SQL("SELECT count(*) AS count FROM public.{}").format(
            sql.Identifier(table_name)
        )
    )
    return cursor.fetchone()["count"]


def _keep_count(cursor, table_name: str) -> int:
    keep_table = KEEP_TABLES[table_name]
    source_column, keep_column = JOIN_COLUMNS[table_name]
    cursor.execute(
        sql.SQL(
            """
            SELECT count(*) AS count
            FROM public.{} AS source
            INNER JOIN {} AS kept
                ON source.{} = kept.{}
            """
        ).format(
            sql.Identifier(table_name),
            sql.Identifier(keep_table),
            sql.Identifier(source_column),
            sql.Identifier(keep_column),
        )
    )
    return cursor.fetchone()["count"]


def _validate_user_references(cursor) -> dict:
    cursor.execute(
        """
        SELECT
            (
                SELECT count(DISTINCT b.article_id)
                FROM public."Project_Article_Bookmark" AS b
                LEFT JOIN temp_keep_article AS kept USING (article_id)
                WHERE kept.article_id IS NULL
            ) AS bookmarks_outside_manifest,
            (
                SELECT count(DISTINCT pj.journal_id)
                FROM public."Project_Journal" AS pj
                LEFT JOIN temp_keep_journal AS kept USING (journal_id)
                WHERE kept.journal_id IS NULL
            ) AS project_journals_outside_closure,
            (
                SELECT count(DISTINCT pk.keyword_id)
                FROM public."Project_Keyword" AS pk
                LEFT JOIN temp_keep_keyword AS kept USING (keyword_id)
                WHERE kept.keyword_id IS NULL
            ) AS project_keywords_outside_closure
        """
    )
    result = dict(cursor.fetchone())
    if any(result.values()):
        raise RuntimeError(
            f"Prospective prune would break user references: {result}"
        )
    return result


def build_prune_report() -> dict:
    connection = psycopg2.connect(
        _database_url(),
        connect_timeout=20,
        application_name="report_manifest_prune",
    )
    try:
        connection.set_session(
            isolation_level="REPEATABLE READ",
            readonly=False,
            autocommit=False,
        )
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '10min'")
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                ("tool-crawl-scimago:manifest-prune",),
            )
            if not cursor.fetchone()["acquired"]:
                raise RuntimeError("Another manifest prune/report is running.")

            manifest = _active_manifest(cursor)
            print(
                f"[1] Active manifest: {manifest['manifest_name']} "
                f"({manifest['selected_count']:,} Article IDs)"
            )
            _create_closure(cursor, manifest["manifest_name"])
            reference_validation = _validate_user_references(cursor)

            rows = []
            total_delete = 0
            for table_name in REPORT_TABLES:
                total = _table_count(cursor, table_name)
                keep = _keep_count(cursor, table_name)
                delete = total - keep
                if delete < 0:
                    raise RuntimeError(
                        f"Invalid keep count for {table_name}: {keep} > {total}"
                    )
                total_delete += delete
                rows.append(
                    {
                        "table": table_name,
                        "total": total,
                        "keep": keep,
                        "delete": delete,
                    }
                )

            report = {
                "mode": "dry-run",
                "manifest": manifest,
                "user_reference_validation": reference_validation,
                "tables": rows,
                "total_rows_to_delete": total_delete,
            }
            connection.rollback()
            return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report manifest prune counts; never deletes production data."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON.",
    )
    return parser


def _print_table(report: dict) -> None:
    print()
    print(f"{'Table':<43} {'Total':>12} {'Keep':>12} {'Delete':>12}")
    print("-" * 83)
    for row in report["tables"]:
        print(
            f"{row['table']:<43} "
            f"{row['total']:>12,} "
            f"{row['keep']:>12,} "
            f"{row['delete']:>12,}"
        )
    print("-" * 83)
    print(
        f"{'TOTAL ROWS TO DELETE':<43} "
        f"{'':>12} {'':>12} "
        f"{report['total_rows_to_delete']:>12,}"
    )
    print()
    print("[DRY-RUN] No production rows or permanent objects were changed.")


def main() -> int:
    args = _build_parser().parse_args()
    try:
        acquire("migrate_local_to_vercel")
        report = build_prune_report()
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            _print_table(report)
    except Exception as exc:
        print(f"[ERROR] Prune report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
