"""Destructively prune production research data to the active manifest.

Requires an explicit confirmation token and a verified pre-prune backup.
Deletes are child-first, batched, committed independently, and resumable.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env.vercel"
DDL_PATH = REPO_ROOT / "database" / "article_manifest.sql"
TOOLS_DIR = REPO_ROOT / "tools"

load_dotenv(ENV_PATH, override=False)

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire  # noqa: E402
import report_manifest_prune as report  # noqa: E402


PRUNE_STAGES = (
    "Journal_Ranking_Subject_Category",
    "Journal_Ranking",
    "Journal_Subject_Category",
    "Sub_Topic",
    "Keyword_Article",
    "Author_Article",
    "Institution_Author",
    "Article",
    "Author",
    "Institution",
    "Keyword",
    "Issue",
    "Volume",
    "Journal",
)

PARENT_FK_INDEXES = {
    "Journal_Ranking": (
        (
            "idx_jrsc_journal_ranking_id",
            "Journal_Ranking_Subject_Category",
            ("journal_ranking_id",),
        ),
    ),
    "Article": (
        (
            "idx_keyword_article_article_id",
            "Keyword_Article",
            ("article_id",),
        ),
        (
            "idx_project_bookmark_article_id",
            "Project_Article_Bookmark",
            ("article_id",),
        ),
    ),
    "Institution": (
        (
            "idx_institution_author_institution_id",
            "Institution_Author",
            ("institution_id",),
        ),
    ),
    "Keyword": (
        (
            "idx_project_keyword_keyword_id",
            "Project_Keyword",
            ("keyword_id",),
        ),
    ),
    "Volume": (
        (
            "idx_issue_volume_id",
            "Issue",
            ("volume_id",),
        ),
    ),
    "Journal": (
        (
            "idx_project_journal_journal_id",
            "Project_Journal",
            ("journal_id",),
        ),
    ),
}


def _database_url() -> str:
    value = os.getenv("VERCEL_DATABASE_URL", "").strip()
    if not value:
        raise ValueError(f"VERCEL_DATABASE_URL is required in {ENV_PATH}.")
    return value


def _primary_key_columns(cursor, table_name: str) -> list[str]:
    cursor.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints AS tc
        INNER JOIN information_schema.key_column_usage AS kcu
            ON kcu.constraint_name = tc.constraint_name
           AND kcu.constraint_schema = tc.constraint_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (table_name,),
    )
    columns = [row["column_name"] for row in cursor.fetchall()]
    return columns


def _create_delete_queue(
    cursor,
    table_name: str,
) -> tuple[list[str], int, bool]:
    cursor.execute("DROP TABLE IF EXISTS temp_prune_queue")
    pk_columns = _primary_key_columns(cursor, table_name)
    uses_ctid = not pk_columns
    keep_table = report.KEEP_TABLES[table_name]
    source_column, keep_column = report.JOIN_COLUMNS[table_name]
    if uses_ctid:
        queue_columns = ["source_ctid"]
        select_columns = sql.SQL("source.ctid AS source_ctid")
    else:
        queue_columns = pk_columns
        select_columns = sql.SQL(", ").join(
            sql.SQL("source.{}").format(sql.Identifier(column))
            for column in pk_columns
        )
    cursor.execute(
        sql.SQL(
            """
            CREATE TEMP TABLE temp_prune_queue
            ON COMMIT PRESERVE ROWS
            AS
            SELECT {}
            FROM public.{} AS source
            LEFT JOIN {} AS kept
                ON source.{} = kept.{}
            WHERE kept.{} IS NULL
            """
        ).format(
            select_columns,
            sql.Identifier(table_name),
            sql.Identifier(keep_table),
            sql.Identifier(source_column),
            sql.Identifier(keep_column),
            sql.Identifier(keep_column),
        )
    )
    cursor.execute(
        sql.SQL("CREATE UNIQUE INDEX ON temp_prune_queue ({})").format(
            sql.SQL(", ").join(
                sql.Identifier(column) for column in queue_columns
            )
        )
    )
    cursor.execute("ANALYZE temp_prune_queue")
    cursor.execute("SELECT count(*) AS count FROM temp_prune_queue")
    return queue_columns, cursor.fetchone()["count"], uses_ctid


def _delete_batch(
    cursor,
    table_name: str,
    queue_columns: list[str],
    batch_size: int,
    uses_ctid: bool,
) -> int:
    returning_columns = sql.SQL(", ").join(
        sql.Identifier(column) for column in queue_columns
    )
    if uses_ctid:
        match_clause = sql.SQL("source.ctid = batch.source_ctid")
    else:
        match_clause = sql.SQL(" AND ").join(
            sql.SQL("source.{} = batch.{}").format(
                sql.Identifier(column),
                sql.Identifier(column),
            )
            for column in queue_columns
        )
    cursor.execute(
        sql.SQL(
            """
            WITH batch AS (
                DELETE FROM temp_prune_queue
                WHERE ctid IN (
                    SELECT ctid
                    FROM temp_prune_queue
                    LIMIT %s
                )
                RETURNING {}
            )
            DELETE FROM public.{} AS source
            USING batch
            WHERE {}
            """
        ).format(
            returning_columns,
            sql.Identifier(table_name),
            match_clause,
        ),
        (batch_size,),
    )
    return cursor.rowcount


def _initial_report(cursor, manifest: dict) -> dict:
    rows = []
    total_delete = 0
    for table_name in report.REPORT_TABLES:
        total = report._table_count(cursor, table_name)
        keep = report._keep_count(cursor, table_name)
        delete = total - keep
        rows.append(
            {
                "table": table_name,
                "total": total,
                "keep": keep,
                "delete": delete,
            }
        )
        total_delete += delete
    return {
        "manifest": manifest,
        "tables": rows,
        "total_rows_to_delete": total_delete,
    }


def _ensure_run(
    cursor,
    manifest: dict,
    backup_path: Path,
    batch_size: int,
    initial_report: dict,
) -> tuple[str, dict]:
    run_name = f"prune-{manifest['manifest_name']}"
    cursor.execute(DDL_PATH.read_text(encoding="utf-8"))
    cursor.execute(
        """
        SELECT *
        FROM pipeline.article_prune_runs
        WHERE run_name = %s
        """,
        (run_name,),
    )
    existing = cursor.fetchone()
    if existing:
        existing = dict(existing)
        if existing["manifest_checksum"] != manifest["selection_checksum"]:
            raise RuntimeError("Manifest checksum changed since prune began.")
        if existing["status"] == "completed":
            print(f"[resume] Run {run_name} is already completed.")
            return run_name, existing["deleted_rows"]
        deleted_rows = existing["deleted_rows"] or {}
        cursor.execute(
            """
            UPDATE pipeline.article_prune_runs
            SET status = 'running',
                backup_path = %s,
                batch_size = %s,
                updated_at = now(),
                last_error = NULL
            WHERE run_name = %s
            """,
            (str(backup_path), batch_size, run_name),
        )
        return run_name, deleted_rows

    cursor.execute(
        """
        INSERT INTO pipeline.article_prune_runs (
            run_name, manifest_name, manifest_checksum, status,
            current_stage, deleted_rows, initial_report, backup_path,
            batch_size
        )
        VALUES (%s, %s, %s, 'running', NULL, '{}'::jsonb, %s, %s, %s)
        """,
        (
            run_name,
            manifest["manifest_name"],
            manifest["selection_checksum"],
            Json(initial_report),
            str(backup_path),
            batch_size,
        ),
    )
    return run_name, {}


def _update_checkpoint(
    cursor,
    run_name: str,
    stage: str,
    deleted_rows: dict,
    status: str = "running",
    error: str | None = None,
) -> None:
    cursor.execute(
        """
        UPDATE pipeline.article_prune_runs
        SET status = %s,
            current_stage = %s,
            deleted_rows = %s,
            updated_at = now(),
            completed_at = CASE WHEN %s = 'completed' THEN now() ELSE NULL END,
            last_error = %s
        WHERE run_name = %s
        """,
        (
            status,
            stage,
            Json(deleted_rows),
            status,
            error,
            run_name,
        ),
    )


def _ensure_parent_fk_indexes(cursor, parent_table: str) -> None:
    indexes = PARENT_FK_INDEXES.get(parent_table, ())
    if not indexes:
        return
    print(f"[index] Ensuring child FK indexes before {parent_table} deletion...")
    for index_name, child_table, columns in indexes:
        cursor.execute(
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON public.{} ({})"
            ).format(
                sql.Identifier(index_name),
                sql.Identifier(child_table),
                sql.SQL(", ").join(
                    sql.Identifier(column) for column in columns
                ),
            )
        )


def _verify_final_state(cursor, manifest: dict) -> dict:
    expected_counts = {
        row["table"]: row["keep"]
        for row in _initial_report(cursor, manifest)["tables"]
    }
    # The current state is already pruned, so every total must equal keep.
    actual_counts = {
        table_name: report._table_count(cursor, table_name)
        for table_name in report.REPORT_TABLES
    }
    mismatches = {
        table: {
            "actual": actual_counts[table],
            "expected_current_keep": expected_counts[table],
        }
        for table in actual_counts
        if actual_counts[table] != expected_counts[table]
    }
    if mismatches:
        raise RuntimeError(f"Post-prune count mismatch: {mismatches}")

    cursor.execute(
        """
        SELECT count(*) AS missing
        FROM pipeline.article_manifest_items AS manifest_item
        LEFT JOIN public."Article" AS article
            ON article.article_id = manifest_item.article_id
        WHERE manifest_item.manifest_name = %s
          AND article.article_id IS NULL
        """,
        (manifest["manifest_name"],),
    )
    missing_manifest_articles = cursor.fetchone()["missing"]
    if missing_manifest_articles:
        raise RuntimeError(
            f"{missing_manifest_articles} manifest Article rows are missing."
        )

    user_references = report._validate_user_references(cursor)
    return {
        "counts": actual_counts,
        "missing_manifest_articles": missing_manifest_articles,
        "user_references": user_references,
    }


def apply_prune(
    backup_path: Path,
    batch_size: int,
    confirmation: str,
) -> dict:
    backup_path = backup_path.resolve()
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise ValueError(f"Backup file is missing or empty: {backup_path}")
    if batch_size <= 0:
        raise ValueError("--batch-size must be greater than zero.")

    connection = psycopg2.connect(
        _database_url(),
        connect_timeout=20,
        application_name="apply_manifest_prune",
    )
    connection.autocommit = False
    run_name = None
    deleted_rows = {}
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("SET statement_timeout = '5min'")
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                ("tool-crawl-scimago:manifest-prune",),
            )
            if not cursor.fetchone()["acquired"]:
                raise RuntimeError("Another manifest prune/report is running.")

            manifest = report._active_manifest(cursor)
            expected_confirmation = f"PRUNE-{manifest['manifest_name']}"
            if confirmation != expected_confirmation:
                raise ValueError(
                    f"--confirm must equal {expected_confirmation!r}."
                )

            report._create_closure(cursor, manifest["manifest_name"])
            report._validate_user_references(cursor)
            initial_report = _initial_report(cursor, manifest)
            run_name, deleted_rows = _ensure_run(
                cursor,
                manifest,
                backup_path,
                batch_size,
                initial_report,
            )
            connection.commit()

            for stage in PRUNE_STAGES:
                if stage in PARENT_FK_INDEXES:
                    _ensure_parent_fk_indexes(cursor, stage)
                    connection.commit()

                queue_columns, queued, uses_ctid = _create_delete_queue(
                    cursor,
                    stage,
                )
                connection.commit()
                already_deleted = int(deleted_rows.get(stage, 0))
                print(
                    f"[stage] {stage}: remaining={queued:,}, "
                    f"previously_deleted={already_deleted:,}"
                )
                stage_deleted = 0
                while True:
                    deleted = _delete_batch(
                        cursor,
                        stage,
                        queue_columns,
                        batch_size,
                        uses_ctid,
                    )
                    if deleted == 0:
                        connection.rollback()
                        break
                    stage_deleted += deleted
                    deleted_rows[stage] = already_deleted + stage_deleted
                    _update_checkpoint(
                        cursor,
                        run_name,
                        stage,
                        deleted_rows,
                    )
                    connection.commit()
                    if stage_deleted % (batch_size * 10) == 0 or deleted < batch_size:
                        print(
                            f"  [{stage}] deleted this run={stage_deleted:,}/"
                            f"{queued:,}, total={deleted_rows[stage]:,}",
                            flush=True,
                        )

            verification = _verify_final_state(cursor, manifest)
            for table_name in report.REPORT_TABLES:
                cursor.execute(
                    sql.SQL("ANALYZE public.{}").format(
                        sql.Identifier(table_name)
                    )
                )
            _update_checkpoint(
                cursor,
                run_name,
                "verification",
                deleted_rows,
                status="completed",
            )
            connection.commit()
            print("[complete] Manifest prune completed and verified.")
            return {
                "run_name": run_name,
                "manifest": manifest,
                "deleted_rows": deleted_rows,
                "verification": verification,
            }
    except KeyboardInterrupt:
        connection.rollback()
        if run_name:
            try:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    _update_checkpoint(
                        cursor,
                        run_name,
                        "interrupted",
                        deleted_rows,
                        status="paused",
                        error="Interrupted by operator",
                    )
                connection.commit()
            except Exception:
                connection.rollback()
        raise
    except Exception as exc:
        connection.rollback()
        if run_name:
            try:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    _update_checkpoint(
                        cursor,
                        run_name,
                        "failed",
                        deleted_rows,
                        status="failed",
                        error=str(exc)[:2000],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
        raise
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Destructively prune production to the active manifest."
    )
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument("--confirm", required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        acquire("migrate_local_to_vercel")
        apply_prune(
            backup_path=args.backup,
            batch_size=args.batch_size,
            confirmation=args.confirm,
        )
    except Exception as exc:
        print(f"[ERROR] Manifest prune failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
