"""
Unified Migration Middleware for Scimago & Vietnam Journals Pipeline.

Provides resilient, resumable database migration from Local DB to Target DB
(e.g., Remote / Supabase / ResearchPulse).

Key features:
1. Resumes interrupted migrations without restarting from scratch.
2. Skips already-migrated tables and duplicate rows via smart ID offset and ON CONFLICT DO NOTHING.
3. Automatically ensures target schema has all required tables and columns (including VN extensions).
4. Unifies migration logic for both Global Scimago and Vietnam Journals branches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
from psycopg2.extras import Json, execute_values
from sqlalchemy import create_engine, pool, text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.import_one_journal_supabase import load_env

try:
    from pipeline_lock import acquire as acquire_lock
except ImportError:
    try:
        from tools.pipeline_lock import acquire as acquire_lock
    except ImportError:
        acquire_lock = lambda name: None

# Tables in strict foreign key topological order
ALL_TABLES: List[str] = [
    "Zone",
    "Subject_Area",
    "Subject_Category",
    "Publisher",
    "Ranking_Metric",
    "Topic",
    "Institution",  # VN extension
    "Journal",
    "Journal_Subject_Category",
    "Volume",
    "Issue",
    "Article",
    "Author",
    "Author_Article",
    "Institution_Author",  # VN extension
    "Keyword",
    "Keyword_Article",
    "Sub_Topic",
    "Journal_Ranking",
    "Journal_Ranking_Subject_Category",
    "Article_Citing_Work",  # VN extension
    "Article_Reference",  # VN extension
]

GLOBAL_TABLES: List[str] = [
    "Zone",
    "Subject_Area",
    "Subject_Category",
    "Publisher",
    "Ranking_Metric",
    "Topic",
    "Journal",
    "Journal_Subject_Category",
    "Volume",
    "Issue",
    "Article",
    "Author",
    "Author_Article",
    "Keyword",
    "Keyword_Article",
    "Sub_Topic",
    "Journal_Ranking",
    "Journal_Ranking_Subject_Category",
]

VN_TABLES: List[str] = [
    "Institution",
    "Institution_Author",
    "Article_Citing_Work",
    "Article_Reference",
]

TABLE_CHUNK_SIZES: Dict[str, int] = {
    "Article": 2000,
    "Author": 10000,
    "Journal": 5000,
    "Keyword_Article": 15000,
    "Author_Article": 15000,
    "Sub_Topic": 15000,
    "Journal_Ranking": 15000,
    "Journal_Subject_Category": 10000,
    "Journal_Ranking_Subject_Category": 10000,
    "Volume": 5000,
    "Issue": 5000,
    "Keyword": 5000,
    "Institution": 5000,
    "Institution_Author": 10000,
    "Article_Citing_Work": 5000,
    "Article_Reference": 5000,
}
DEFAULT_CHUNK_SIZE: int = 5000


def resolve_migration_urls(src_override: Optional[str] = None, tgt_override: Optional[str] = None) -> Tuple[str, str]:
    """Resolve source and target database URLs from arguments or environment."""
    load_env()

    # Resolve Source URL (Local DB priority)
    src_url = src_override or os.getenv("OLD_DATABASE_URL") or os.getenv("LOCAL_DATABASE_URL")
    if not src_url:
        db_url = os.getenv("DATABASE_URL")
        if db_url and ("localhost" in db_url or "127.0.0.1" in db_url):
            src_url = db_url
        else:
            src_url = "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db"

    # Resolve Target URL (Remote / ResearchPulse priority)
    tgt_url = tgt_override or os.getenv("NEW_DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not tgt_url:
        db_url = os.getenv("DATABASE_URL")
        if db_url and "localhost" not in db_url and "127.0.0.1" not in db_url:
            tgt_url = db_url
        else:
            tgt_url = "postgresql+psycopg2://postgres:postgres123@100.121.61.95:5432/researchpulse"

    return src_url, tgt_url


def make_engine(url: str):
    """Build SQLAlchemy engine with resilient connection pool settings."""
    return create_engine(
        url,
        poolclass=pool.QueuePool,
        pool_size=3,
        max_overflow=1,
        pool_recycle=600,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 30,
            "options": "-c statement_timeout=300000 -c synchronous_commit=off",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )


class MigrationMiddleware:
    """
    Unified migration controller for both Global Scimago and Vietnam Journals.
    Handles resume tracking, diff skipping, connection resilience, and sequence sync.
    """

    def __init__(
        self,
        src_url: Optional[str] = None,
        tgt_url: Optional[str] = None,
        branch: str = "all",
        resume: bool = True,
        clean: bool = False,
        tables: Optional[List[str]] = None,
        checkpoint_file: Optional[Path] = None,
    ):
        self.src_url, self.tgt_url = resolve_migration_urls(src_url, tgt_url)
        self.branch = branch.lower()
        self.resume = resume and not clean
        self.clean = clean
        self.custom_tables = tables
        self.checkpoint_file = checkpoint_file or (REPO_ROOT / "logs" / "migration_checkpoint.json")
        self.checkpoint_data: Dict[str, Any] = {}

        self.src_engine = make_engine(self.src_url)
        self.tgt_engine = make_engine(self.tgt_url)

    def load_checkpoint(self) -> None:
        """Load migration checkpoint from disk if resuming."""
        if self.resume and self.checkpoint_file.exists():
            try:
                self.checkpoint_data = json.loads(self.checkpoint_file.read_text(encoding="utf-8"))
                print(f"[checkpoint] Loaded previous state from {self.checkpoint_file.name}")
            except Exception as e:
                print(f"[checkpoint] Warning: Failed to parse checkpoint file: {e}")
                self.checkpoint_data = {}
        else:
            self.checkpoint_data = {
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source_url": self._mask_url(self.src_url),
                "target_url": self._mask_url(self.tgt_url),
                "branch": self.branch,
                "tables": {},
            }

    def save_checkpoint(
        self,
        table_name: str,
        status: str,
        last_pk_value: Any = None,
        migrated_rows: int = 0,
        total_rows: int = 0,
    ) -> None:
        """Persist table migration state to checkpoint file."""
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        if "tables" not in self.checkpoint_data:
            self.checkpoint_data["tables"] = {}

        self.checkpoint_data["tables"][table_name] = {
            "status": status,
            "last_pk_value": last_pk_value,
            "migrated_rows": migrated_rows,
            "total_rows": total_rows,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            temp_path = self.checkpoint_file.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self.checkpoint_data, indent=2), encoding="utf-8")
            temp_path.replace(self.checkpoint_file)
        except Exception:
            pass

    @staticmethod
    def _mask_url(url: str) -> str:
        if "@" in url:
            prefix, host = url.split("@", 1)
            scheme = prefix.split("://")[0] if "://" in prefix else "postgresql"
            return f"{scheme}://***@{host}"
        return url

    def get_target_tables(self) -> List[str]:
        """Determine tables to migrate according to branch or user filter."""
        if self.custom_tables:
            return [t for t in ALL_TABLES if t in self.custom_tables or t.lower() in [ct.lower() for ct in self.custom_tables]]

        if self.branch == "global":
            return GLOBAL_TABLES
        if self.branch == "vn":
            # VN branch requires core journal/article structure plus VN tables
            return [
                "Publisher",
                "Topic",
                "Institution",
                "Journal",
                "Volume",
                "Issue",
                "Article",
                "Author",
                "Author_Article",
                "Institution_Author",
                "Keyword",
                "Keyword_Article",
                "Sub_Topic",
                "Article_Citing_Work",
                "Article_Reference",
            ]
        return ALL_TABLES

    @staticmethod
    def get_columns(conn, table_name: str) -> Dict[str, str]:
        rows = conn.execute(
            text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :tbl
                ORDER BY ordinal_position
            """),
            {"tbl": table_name},
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    def get_pk_columns(conn, table_name: str) -> List[str]:
        rows = conn.execute(
            text("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = 'public'
                  AND tc.table_name = :tbl
                  AND tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
            """),
            {"tbl": table_name},
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_row_count(conn, table_name: str) -> int:
        try:
            return conn.execute(text(f'SELECT count(*) FROM "{table_name}"')).scalar() or 0
        except Exception:
            return 0

    def ensure_target_schema(self) -> None:
        """Auto-apply VN schema extensions to target if not yet present."""
        print("[schema] Checking target schema compatibility...")
        with self.tgt_engine.begin() as conn:
            # 1. Check/create Institution
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS "Institution" (
                  "institution_id" BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                  "openalex_id" varchar UNIQUE,
                  "display_name" varchar NOT NULL,
                  "country_code" varchar,
                  "type" varchar,
                  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP,
                  "is_deleted" boolean DEFAULT false,
                  CONSTRAINT "uq_institution_name_country_type" UNIQUE ("display_name", "country_code", "type")
                );
                CREATE INDEX IF NOT EXISTS "idx_institution_country_type" ON "Institution" ("country_code", "type");
            """))

            # 2. Check/create Institution_Author
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS "Institution_Author" (
                  "author_id" bigint NOT NULL,
                  "institution_id" bigint NOT NULL,
                  "year" int NOT NULL,
                  PRIMARY KEY ("author_id", "institution_id", "year")
                );
                CREATE INDEX IF NOT EXISTS "idx_institution_author_institution" ON "Institution_Author" ("institution_id");
                CREATE INDEX IF NOT EXISTS "idx_institution_author_year" ON "Institution_Author" ("year");
            """))

            # 3. Check/create Article_Citing_Work
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS "Article_Citing_Work" (
                  "article_id" bigint NOT NULL,
                  "openalex_work_id" varchar NOT NULL,
                  "citing_article_id" bigint,
                  "doi" varchar,
                  "title" text,
                  "publication_year" int,
                  "source_name" varchar,
                  "source_url" text,
                  "landing_url" text,
                  "pdf_url" text,
                  "cited_by_count" bigint,
                  "type" varchar,
                  "authors" jsonb,
                  "raw" jsonb,
                  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP,
                  "updated_at" timestamp DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY ("article_id", "openalex_work_id")
                );
                CREATE INDEX IF NOT EXISTS "idx_citing_work_article" ON "Article_Citing_Work" ("article_id");
            """))

            # 4. Check/create Article_Reference
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS "Article_Reference" (
                  "article_id" bigint NOT NULL,
                  "reference_key" varchar NOT NULL,
                  "openalex_work_id" varchar,
                  "semantic_scholar_id" varchar,
                  "doi" varchar,
                  "title" text,
                  "publication_year" int,
                  "source_name" varchar,
                  "source_url" text,
                  "landing_url" text,
                  "pdf_url" text,
                  "cited_by_count" bigint,
                  "type" varchar,
                  "authors" jsonb,
                  "raw" jsonb,
                  "referenced_article_id" bigint,
                  "created_at" timestamp DEFAULT CURRENT_TIMESTAMP,
                  "updated_at" timestamp DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY ("article_id", "reference_key")
                );
                CREATE INDEX IF NOT EXISTS "idx_reference_article" ON "Article_Reference" ("article_id");
            """))

            # 5. Add extra columns to existing tables
            conn.execute(text('ALTER TABLE "Journal" ADD COLUMN IF NOT EXISTS "owning_institution" varchar;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "openalex_id" varchar;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "landing_url" varchar;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "pdf_url" varchar;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "pages" varchar;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "is_open_access" boolean;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "citing_patents_count" bigint;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "citations_by_year" jsonb;'))
            conn.execute(text('ALTER TABLE "Article" ADD COLUMN IF NOT EXISTS "is_vn_journal" boolean DEFAULT false;'))
            conn.execute(text('ALTER TABLE "Author_Article" ADD COLUMN IF NOT EXISTS "author_position" varchar;'))
        print("[schema] Target schema verified and ready.")

    def prepare_target(self) -> None:
        """Drop heavy GIN indexes on target for faster bulk inserts, clean if requested."""
        if self.clean:
            print("\n[prepare] Cleaning academic tables (reverse FK order)...")
            with self.tgt_engine.begin() as conn:
                for tbl in reversed(ALL_TABLES):
                    try:
                        conn.execute(text(f'TRUNCATE TABLE "{tbl}" CASCADE'))
                        print(f"  Truncated {tbl}")
                    except Exception as e:
                        print(f"  Warning truncating {tbl}: {e}")
            self.checkpoint_data["tables"] = {}

        print("\n[prepare] Dropping GIN text search indexes on Article for insert performance...")
        with self.tgt_engine.begin() as conn:
            conn.execute(text('DROP INDEX IF EXISTS idx_article_abstract_trgm'))
            conn.execute(text('DROP INDEX IF EXISTS idx_article_title_trgm'))
        print("  GIN trgm indexes dropped (will be rebuilt at completion).")

    def _get_fresh_tgt_conn(self, max_retries: int = 10):
        """Create fresh psycopg2 raw connection with timeouts and retry."""
        for i in range(1, max_retries + 1):
            try:
                self.tgt_engine.dispose()
                conn = self.tgt_engine.raw_connection()
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 300000;")
                    cur.execute("SET synchronous_commit = off;")
                conn.commit()
                return conn
            except Exception as ex:
                wait_time = min(30, 3 * i)
                print(f"  [RETRY] Target DB connection {i}/{max_retries} failed: {ex}. Retrying in {wait_time}s...")
                if i == max_retries:
                    raise
                time.sleep(wait_time)

    def _execute_batch_with_retry(self, conn_ref: List[Any], insert_query: str, batch: List[Tuple], max_retries: int = 10):
        """Execute batch insert with automatic reconnection and rollback."""
        for attempt in range(1, max_retries + 1):
            try:
                with conn_ref[0].cursor() as cur:
                    execute_values(cur, insert_query, batch, page_size=len(batch))
                conn_ref[0].commit()
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError) as ex:
                print(f"  [WARN] Batch insert error on attempt {attempt}/{max_retries}: {ex}")
                try:
                    conn_ref[0].rollback()
                except Exception:
                    pass
                try:
                    conn_ref[0].close()
                except Exception:
                    pass
                if attempt == max_retries:
                    raise
                wait_time = min(30, 3 * attempt)
                print(f"  [RETRY] Reconnecting target DB in {wait_time}s...")
                time.sleep(wait_time)
                conn_ref[0] = self._get_fresh_tgt_conn()

    def migrate_table(self, table_name: str) -> None:
        """Migrate a single table with resume capability and existing-data skipping."""
        print(f"\n========================================================")
        print(f"[*] Processing Table: {table_name}")
        print(f"========================================================")

        with self.src_engine.connect() as src_conn, self.tgt_engine.connect() as tgt_conn:
            src_cols_info = self.get_columns(src_conn, table_name)
            tgt_cols_info = self.get_columns(tgt_conn, table_name)

            if not src_cols_info or not tgt_cols_info:
                print(f"  [SKIP] Table '{table_name}' missing in source or target.")
                return

            common_cols = [c for c in src_cols_info if c in tgt_cols_info]
            total_src_rows = self.get_row_count(src_conn, table_name)
            tgt_start_rows = self.get_row_count(tgt_conn, table_name)
            print(f"  Columns mapped ({len(common_cols)}): {common_cols}")
            print(f"  Source rows: {total_src_rows:,} | Target current rows: {tgt_start_rows:,}")

            if total_src_rows == 0:
                print(f"  Source table is empty. Skipping.")
                self.save_checkpoint(table_name, "completed", None, 0, 0)
                return

            # Checkpoint check: if already completed and target has >= source rows
            prev_table_state = self.checkpoint_data.get("tables", {}).get(table_name, {})
            if self.resume and prev_table_state.get("status") == "completed" and tgt_start_rows >= total_src_rows:
                print(f"  [SKIP] Table already completed in previous checkpoint ({tgt_start_rows:,} rows).")
                return

            if self.resume and tgt_start_rows == total_src_rows and total_src_rows > 0:
                print(f"  [SKIP] Target already has identical row count ({tgt_start_rows:,}).")
                self.save_checkpoint(table_name, "completed", None, tgt_start_rows, total_src_rows)
                return

            pks = self.get_pk_columns(tgt_conn, table_name)

        chunk_size = TABLE_CHUNK_SIZES.get(table_name, DEFAULT_CHUNK_SIZE)
        quoted_cols = ", ".join([f'"{c}"' for c in common_cols])
        json_cols = {c for c in common_cols if tgt_cols_info[c] in ("json", "jsonb")}

        # Single numeric primary key optimization for resume
        single_num_pk: Optional[str] = None
        max_tgt_pk_val = 0
        where_clause = ""
        query_params: Dict[str, Any] = {}

        if self.resume and len(pks) == 1 and pks[0] in common_cols:
            pk = pks[0]
            if tgt_cols_info.get(pk) in ("bigint", "integer", "smallint"):
                single_num_pk = pk
                with self.tgt_engine.connect() as tgt_conn:
                    max_tgt_pk_val = tgt_conn.execute(text(f'SELECT COALESCE(MAX("{pk}"), 0) FROM "{table_name}"')).scalar() or 0
                if max_tgt_pk_val > 0:
                    where_clause = f'WHERE "{pk}" > :min_pk'
                    query_params["min_pk"] = max_tgt_pk_val
                    print(f"  [RESUME] Target already has records up to {single_num_pk}={max_tgt_pk_val:,}. Streaming only new rows...")
        elif self.resume and len(pks) > 1 and "article_id" in pks and "article_id" in common_cols:
            if self.branch == "vn":
                where_clause = 'WHERE "article_id" IN (SELECT "article_id" FROM "Article" WHERE "is_vn_journal" = TRUE)'
                print(f"  [RESUME-VN] Streaming link rows for VN articles only...")
            else:
                art_state = self.checkpoint_data.get("tables", {}).get("Article", {})
                min_art_id = art_state.get("last_pk_value")
                if min_art_id and min_art_id > 0:
                    where_clause = f'WHERE "article_id" > :min_art_id'
                    query_params["min_art_id"] = min_art_id
                    print(f"  [RESUME] Target already has records up to article_id={min_art_id:,}. Streaming only new rows...")

        insert_query = f"""
            INSERT INTO "{table_name}" ({quoted_cols})
            VALUES %s
            ON CONFLICT DO NOTHING
        """
        select_sql = f'SELECT {quoted_cols} FROM "{table_name}" {where_clause} ORDER BY {quoted_cols.split(",")[0]} ASC'

        t_start = time.time()
        migrated_rows = tgt_start_rows
        new_rows_processed = 0
        batch_num = 0
        last_pk_value = max_tgt_pk_val if single_num_pk else None

        conn_ref = [self._get_fresh_tgt_conn()]

        try:
            with self.src_engine.connect().execution_options(stream_results=True, yield_per=chunk_size) as src_conn:
                res = src_conn.execute(text(select_sql), query_params)
                batch = []

                for row in res:
                    row_val = list(row)
                    if json_cols:
                        for i, col in enumerate(common_cols):
                            if col in json_cols and row_val[i] is not None:
                                if isinstance(row_val[i], (dict, list)):
                                    row_val[i] = Json(row_val[i])

                    batch.append(tuple(row_val))

                    if single_num_pk:
                        pk_idx = common_cols.index(single_num_pk)
                        last_pk_value = row_val[pk_idx]

                    if len(batch) >= chunk_size:
                        batch_num += 1
                        t0 = time.time()
                        self._execute_batch_with_retry(conn_ref, insert_query, batch, max_retries=10)
                        t_batch = time.time() - t0
                        migrated_rows += len(batch)
                        new_rows_processed += len(batch)
                        pct = (migrated_rows / total_src_rows) * 100 if total_src_rows > 0 else 100.0
                        elapsed = time.time() - t_start
                        rps = new_rows_processed / elapsed if elapsed > 0 else 0
                        remaining = max(0, total_src_rows - migrated_rows)
                        eta_sec = remaining / rps if rps > 0 else 0

                        print(
                            f"  -> Batch {batch_num:3d}: {migrated_rows:,}/{total_src_rows:,} ({pct:5.1f}%) "
                            f"| {len(batch)} rows in {t_batch:.2f}s | Speed: {rps:,.0f} r/s | ETA: {eta_sec:.0f}s"
                        )
                        self.save_checkpoint(table_name, "in_progress", last_pk_value, migrated_rows, total_src_rows)
                        batch = []

                if batch:
                    batch_num += 1
                    t0 = time.time()
                    self._execute_batch_with_retry(conn_ref, insert_query, batch, max_retries=10)
                    t_batch = time.time() - t0
                    migrated_rows += len(batch)
                    new_rows_processed += len(batch)
                    print(f"  -> Batch {batch_num:3d} (Final): {migrated_rows:,}/{total_src_rows:,} (100.0%) in {t_batch:.2f}s")

        finally:
            try:
                conn_ref[0].close()
            except Exception:
                pass

        total_time = time.time() - t_start
        with self.tgt_engine.connect() as tgt_conn:
            tgt_final_rows = self.get_row_count(tgt_conn, table_name)

        self.save_checkpoint(table_name, "completed", last_pk_value, tgt_final_rows, total_src_rows)
        print(f"[OK] Completed '{table_name}' in {total_time:.1f}s. Target rows: {tgt_final_rows:,}")

    def reset_sequences(self, tables_to_check: Optional[List[str]] = None) -> None:
        """Reset PostgreSQL auto-increment sequences to max(id) on target DB."""
        print("\n[sequences] Synchronizing auto-increment identity sequences...")
        tables = tables_to_check or ALL_TABLES
        with self.tgt_engine.begin() as conn:
            for tbl in tables:
                pks = self.get_pk_columns(conn, tbl)
                if len(pks) == 1:
                    pk = pks[0]
                    seq = conn.execute(text(f"SELECT pg_get_serial_sequence('\"{tbl}\"', '{pk}')")).scalar()
                    if seq:
                        max_id = conn.execute(text(f'SELECT COALESCE(MAX("{pk}"), 0) FROM "{tbl}"')).scalar()
                        if max_id and max_id > 0:
                            conn.execute(text(f"SELECT setval('{seq}', {max_id})"))
                            print(f"  Seq '{seq}' set to {max_id:,}")
        print("[sequences] All sequences synchronized.")

    def restore_project_relations(self) -> None:
        """Safely restore Project & Project_Keyword relations if Project table exists."""
        with self.tgt_engine.begin() as conn:
            has_project = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='Project'")
            ).scalar()
            if has_project:
                print("\n[restore] Restoring Project links on target...")
                conn.execute(text("""
                    INSERT INTO "Project" (project_id, user_id, subject_area, title, created_at, status)
                    VALUES (1, 'b97440eb-82f1-4e84-b4de-ebd46087ed00', 44, 'Cancer Biology', '2026-09-03 10:58:55.573431', 'ACTIVE')
                    ON CONFLICT (project_id) DO UPDATE 
                    SET subject_area = 44, title = 'Cancer Biology', status = 'ACTIVE'
                """))
                conn.execute(text("""
                    INSERT INTO "Project_Keyword" (project_id, keyword_id)
                    VALUES (1, 2), (1, 15)
                    ON CONFLICT DO NOTHING
                """))
                print("[restore] Project relations restored.")

    def rebuild_indexes(self) -> None:
        """Rebuild GIN text search indexes on Article."""
        print("\n[indexes] Rebuilding GIN trgm text search indexes on Article...")
        with self.tgt_engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0;"))
            conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON "Article" USING gin (title gin_trgm_ops)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_abstract_trgm ON "Article" USING gin (abstract gin_trgm_ops)'))
            conn.commit()
        print("[indexes] GIN indexes rebuilt successfully.")

    def verify(self, tables_to_check: Optional[List[str]] = None) -> bool:
        """Compare and print audit table of source vs target row counts."""
        print("\n================================================================")
        print("                 MIGRATION AUDIT & VERIFICATION                 ")
        print("================================================================")
        print(f"{'Table Name':<35} {'Source Rows':>15} {'Target Rows':>15} {'Status':>10}")
        print("-" * 78)

        tables = tables_to_check or ALL_TABLES
        all_matched = True
        with self.src_engine.connect() as sc, self.tgt_engine.connect() as tc:
            for tbl in tables:
                s_cnt = self.get_row_count(sc, tbl)
                t_cnt = self.get_row_count(tc, tbl)
                status = "MATCH" if s_cnt == t_cnt else ("DIFF" if t_cnt > 0 else "EMPTY")
                if s_cnt != t_cnt:
                    all_matched = False
                print(f"{tbl:<35} {s_cnt:>15,} {t_cnt:>15,} {status:>10}")

        print("=" * 78)
        if all_matched:
            print("[SUCCESS] ALL TABLES MATCH 100% WITH SOURCE DATABASE!")
        else:
            print("[NOTICE] Target has row count variations as noted above.")
        return all_matched

    def run(self) -> None:
        """Execute end-to-end migration."""
        acquire_lock("migrate_to_researchpulse")

        print("================================================================")
        print("         UNIFIED DATABASE MIGRATION (LOCAL -> REMOTE)           ")
        print("================================================================")
        print(f"SOURCE: {self._mask_url(self.src_url)}")
        print(f"TARGET: {self._mask_url(self.tgt_url)}")
        print(f"BRANCH: {self.branch.upper()}")
        print(f"MODE:   {'RESUME (Skip existing data)' if self.resume else 'CLEAN START'}")
        print("================================================================")

        with self.src_engine.connect() as c1, self.tgt_engine.connect() as c2:
            db1 = c1.execute(text("SELECT current_database()")).scalar()
            db2 = c2.execute(text("SELECT current_database()")).scalar()
            print(f"Source connected: {db1}")
            print(f"Target connected: {db2}")

        t_global_start = time.time()
        self.load_checkpoint()

        # Step 1: Ensure Target Schema
        self.ensure_target_schema()

        # Step 2: Prepare target
        self.prepare_target()

        # Step 3: Migrate tables in topological order
        tables_to_run = self.get_target_tables()
        for tbl in tables_to_run:
            self.migrate_table(tbl)

        # Step 4: Restore relations & sequences
        self.restore_project_relations()
        self.reset_sequences(tables_to_run)

        # Step 5: Rebuild indexes
        if "Article" in tables_to_run:
            self.rebuild_indexes()

        # Step 6: Verify
        self.verify(tables_to_run)

        total_elapsed = time.time() - t_global_start
        print("\n================================================================")
        print(f"[FINISHED] Migration completed in {total_elapsed / 60:.2f} minutes!")
        print("================================================================")


def main_cli():
    parser = argparse.ArgumentParser(description="Unified Migration Middleware (Local DB -> Target DB)")
    parser.add_argument("--branch", choices=["all", "global", "vn"], default="all", help="Pipeline branch to migrate (default: all)")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume from interruption and skip existing data (default: True)")
    parser.add_argument("--clean", action="store_true", help="Force clean start (truncate academic tables on target)")
    parser.add_argument("--table", nargs="+", help="Specific table(s) to migrate")
    parser.add_argument("--source", help="Custom source PostgreSQL URL")
    parser.add_argument("--target", help="Custom target PostgreSQL URL")
    args = parser.parse_args()

    middleware = MigrationMiddleware(
        src_url=args.source,
        tgt_url=args.target,
        branch=args.branch,
        resume=not args.clean,
        clean=args.clean,
        tables=args.table,
    )
    middleware.run()


if __name__ == "__main__":
    main_cli()
