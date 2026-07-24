"""Create a compressed, credential-free logical safety archive before prune."""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env.vercel"
TOOLS_DIR = REPO_ROOT / "tools"
DEFAULT_BACKUP_DIR = REPO_ROOT / "backups"

load_dotenv(ENV_PATH, override=False)

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from pipeline_lock import acquire  # noqa: E402


PUBLIC_TABLES = (
    "user",
    "Project",
    "Project_Member",
    "Project_Keyword",
    "Subject_Category_Project",
    "Project_Journal",
    "Project_Article_Bookmark",
    "Project_Chat_Message",
    "Password_Reset_Token",
    "wallet",
    "payment_transaction",
    "wallet_transaction",
    "System_Log",
    "coin_package",
)

MANIFEST_EXPORTS = {
    "selected_Article": """
        SELECT a.*
        FROM public."Article" AS a
        INNER JOIN pipeline.article_manifest_items AS m
            ON m.article_id = a.article_id
        INNER JOIN pipeline.article_manifests AS manifest
            ON manifest.manifest_name = m.manifest_name
           AND manifest.is_active
        ORDER BY a.article_id
    """,
    "selected_Author_Article": """
        SELECT link.*
        FROM public."Author_Article" AS link
        INNER JOIN pipeline.article_manifest_items AS m
            ON m.article_id = link.article_id
        INNER JOIN pipeline.article_manifests AS manifest
            ON manifest.manifest_name = m.manifest_name
           AND manifest.is_active
        ORDER BY link.author_id, link.article_id
    """,
    "selected_Keyword_Article": """
        SELECT link.*
        FROM public."Keyword_Article" AS link
        INNER JOIN pipeline.article_manifest_items AS m
            ON m.article_id = link.article_id
        INNER JOIN pipeline.article_manifests AS manifest
            ON manifest.manifest_name = m.manifest_name
           AND manifest.is_active
        ORDER BY link.keyword_id, link.article_id
    """,
    "selected_Sub_Topic": """
        SELECT link.*
        FROM public."Sub_Topic" AS link
        INNER JOIN pipeline.article_manifest_items AS m
            ON m.article_id = link.article_id
        INNER JOIN pipeline.article_manifests AS manifest
            ON manifest.manifest_name = m.manifest_name
           AND manifest.is_active
        ORDER BY link.article_id, link.topic_id
    """,
    "article_manifests": """
        SELECT * FROM pipeline.article_manifests ORDER BY manifest_name
    """,
    "article_manifest_items": """
        SELECT * FROM pipeline.article_manifest_items
        ORDER BY manifest_name, selected_rank
    """,
}


def _database_url() -> str:
    value = os.getenv("VERCEL_DATABASE_URL", "").strip()
    if not value:
        raise ValueError(f"VERCEL_DATABASE_URL is required in {ENV_PATH}.")
    return value


def _copy_query_to_zip(connection, archive, name: str, query: str) -> int:
    count_query = f"SELECT count(*) FROM ({query}) AS export_rows"
    with connection.cursor() as cursor:
        cursor.execute(count_query)
        row_count = cursor.fetchone()[0]

    copy_query = f"COPY ({query}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)"
    with (
        archive.open(f"data/{name}.csv", "w") as binary_stream,
        io.TextIOWrapper(binary_stream, encoding="utf-8", newline="") as text_stream,
        connection.cursor() as cursor,
    ):
        cursor.copy_expert(copy_query, text_stream)
        text_stream.flush()
    return row_count


def _table_query(connection, table_name: str) -> str:
    with connection.cursor() as cursor:
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
        pk_columns = [row[0] for row in cursor.fetchall()]

    table_sql = sql.Identifier("public", table_name).as_string(connection)
    query = f"SELECT * FROM {table_sql}"
    if pk_columns:
        order_sql = ", ".join(
            sql.Identifier(column).as_string(connection)
            for column in pk_columns
        )
        query += f" ORDER BY {order_sql}"
    return query


def create_backup(output_path: Path) -> dict:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"Backup already exists: {output_path}")

    connection = psycopg2.connect(
        _database_url(),
        connect_timeout=20,
        application_name="backup_pre_prune",
    )
    connection.set_session(
        isolation_level="REPEATABLE READ",
        readonly=True,
        autocommit=False,
    )
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "format": "csv-in-zip",
        "purpose": "pre-manifest-prune safety archive",
        "tables": {},
    }
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT manifest_name, selected_count, source_article_count,
                       algorithm_version, selection_checksum
                FROM pipeline.article_manifests
                WHERE is_active
                """
            )
            manifest = cursor.fetchone()
            if manifest is None:
                raise RuntimeError("No active Article manifest exists.")
            metadata["manifest"] = dict(manifest)

        with zipfile.ZipFile(
            output_path,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for table_name in PUBLIC_TABLES:
                query = _table_query(connection, table_name)
                rows = _copy_query_to_zip(
                    connection,
                    archive,
                    f"public.{table_name}",
                    query,
                )
                metadata["tables"][f"public.{table_name}"] = rows
                print(f"[backup] public.{table_name}: {rows:,} rows")

            for export_name, query in MANIFEST_EXPORTS.items():
                normalized_query = " ".join(query.split())
                rows = _copy_query_to_zip(
                    connection,
                    archive,
                    export_name,
                    normalized_query,
                )
                metadata["tables"][export_name] = rows
                print(f"[backup] {export_name}: {rows:,} rows")

            archive.write(
                REPO_ROOT / "database" / "schema.sql",
                "schema/public_schema.sql",
            )
            archive.write(
                REPO_ROOT / "database" / "article_manifest.sql",
                "schema/article_manifest.sql",
            )
            archive.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
            )
        connection.rollback()
    except Exception:
        connection.rollback()
        if output_path.exists():
            output_path.unlink()
        raise
    finally:
        connection.close()

    digest = hashlib.sha256()
    with output_path.open("rb") as backup_file:
        for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
            digest.update(chunk)
    result = {
        **metadata,
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "sha256": digest.hexdigest(),
    }
    print(
        f"[backup] Complete: {output_path} "
        f"({result['bytes']:,} bytes, sha256={result['sha256']})"
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_path = DEFAULT_BACKUP_DIR / f"pre_prune_20k_{timestamp}.zip"
    parser = argparse.ArgumentParser(description="Backup critical data before prune.")
    parser.add_argument(
        "--output",
        type=Path,
        default=default_path,
        help=f"Backup ZIP path (default: {default_path}).",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        acquire("migrate_local_to_vercel")
        create_backup(args.output)
    except Exception as exc:
        print(f"[ERROR] Backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
