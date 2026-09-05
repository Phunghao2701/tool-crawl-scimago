"""
Migrate complete academic data from Old DB (scientific_journal_db) to New DB (researchpulse).

Run:
  python tools/migrate_to_researchpulse.py
"""
import os
import sys
import time
import json
from sqlalchemy import create_engine, text, pool
import psycopg2
from psycopg2.extras import execute_values, Json

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

OLD_URL = os.getenv("OLD_DATABASE_URL", "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db")
NEW_URL = os.getenv("NEW_DATABASE_URL", "postgresql+psycopg2://postgres:postgres123@100.121.61.95:5432/researchpulse")

TABLES_ORDER = [
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

TABLE_CHUNK_SIZES = {
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
}
DEFAULT_CHUNK_SIZE = 5000


def make_engine(url):
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


def get_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl
        ORDER BY ordinal_position
    """), {"tbl": table_name}).fetchall()
    return {r[0]: r[1] for r in rows}


def get_pk_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = :tbl
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
    """), {"tbl": table_name}).fetchall()
    return [r[0] for r in rows]


def get_row_count(conn, table_name):
    return conn.execute(text(f'SELECT count(*) FROM "{table_name}"')).scalar()


def prepare_target_database(tgt_engine):
    """
    Safely detach Project dependencies, clear old test dummy data in academic tables,
    preserving user, wallet, system_log, Project, and _prisma_migrations.
    """
    print("\n[prepare] Checking Project and user records in Target DB...")
    with tgt_engine.begin() as conn:
        # Check if project exists
        has_project = conn.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='Project'")).scalar()
        if has_project:
            proj_count = conn.execute(text('SELECT count(*) FROM "Project"')).scalar()
            if proj_count > 0:
                print(f"  Preserving {proj_count} Project records. Temporarily detaching FKs...")
                conn.execute(text('UPDATE "Project" SET subject_area = NULL'))
                conn.execute(text('DELETE FROM "Project_Keyword"'))
        
        print("\n[prepare] Cleaning old test data in academic tables (reverse FK order)...")
        for tbl in reversed(TABLES_ORDER):
            conn.execute(text(f'TRUNCATE TABLE "{tbl}" CASCADE'))
            print(f"  Truncated {tbl}")
            
        print("\n[prepare] Dropping GIN indexes on Article to maximize bulk insert speed...")
        conn.execute(text('DROP INDEX IF EXISTS idx_article_abstract_trgm'))
        conn.execute(text('DROP INDEX IF EXISTS idx_article_title_trgm'))
        print("  Dropped GIN trgm indexes (will be rebuilt after migration).")
    print("[prepare] Academic tables cleaned and ready for clean migration.")


def restore_project_relations(tgt_engine):
    print("\n[restore] Restoring Project relations...")
    with tgt_engine.begin() as conn:
        # Insert Project 1 if missing (since TRUNCATE CASCADE might remove it if FK exists)
        conn.execute(text("""
            INSERT INTO "Project" (project_id, user_id, subject_area, title, created_at, status)
            VALUES (1, 'b97440eb-82f1-4e84-b4de-ebd46087ed00', 44, 'Cancer Biology', '2026-09-03 10:58:55.573431', 'ACTIVE')
            ON CONFLICT (project_id) DO UPDATE 
            SET subject_area = 44, title = 'Cancer Biology', status = 'ACTIVE'
        """))
        # Keywords: 2 ('Cancer'), 15 ('Causes of cancer')
        conn.execute(text("""
            INSERT INTO "Project_Keyword" (project_id, keyword_id)
            VALUES (1, 2), (1, 15)
            ON CONFLICT DO NOTHING
        """))
    print("[restore] Project relations successfully linked to migrated data.")


def reset_sequences(target_engine, tables=None):
    print("\n[sequences] Resetting table sequences in target DB...")
    tables_to_check = tables if tables else TABLES_ORDER
    with target_engine.begin() as conn:
        for tbl in tables_to_check:
            pks = get_pk_columns(conn, tbl)
            if len(pks) == 1:
                pk = pks[0]
                seq = conn.execute(text(f"SELECT pg_get_serial_sequence('\"{tbl}\"', '{pk}')")).scalar()
                if seq:
                    max_id = conn.execute(text(f'SELECT COALESCE(MAX("{pk}"), 0) FROM "{tbl}"')).scalar()
                    if max_id and max_id > 0:
                        conn.execute(text(f"SELECT setval('{seq}', {max_id})"))
                        print(f"  Seq '{seq}' reset to {max_id:,}")
    print("[sequences] All sequences synchronized.")


def get_fresh_tgt_conn(tgt_engine, max_retries=10):
    for i in range(1, max_retries + 1):
        try:
            tgt_engine.dispose()
            conn = tgt_engine.raw_connection()
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 300000;")
                cur.execute("SET synchronous_commit = off;")
            conn.commit()
            return conn
        except Exception as ex:
            wait_time = min(30, 3 * i)
            print(f"\n  [RETRY] Target connection attempt {i}/{max_retries} failed: {ex}. Retrying in {wait_time}s...")
            if i == max_retries:
                raise
            time.sleep(wait_time)


def execute_batch_with_retry(tgt_engine, conn_ref, insert_query, batch, max_retries=10):
    for attempt in range(1, max_retries + 1):
        try:
            with conn_ref[0].cursor() as cur:
                execute_values(cur, insert_query, batch, page_size=len(batch))
            conn_ref[0].commit()
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError) as ex:
            print(f"\n  [WARN] Batch insert error on attempt {attempt}/{max_retries}: {ex}")
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
            print(f"  [RETRY] Reconnecting to Target DB in {wait_time}s...")
            time.sleep(wait_time)
            conn_ref[0] = get_fresh_tgt_conn(tgt_engine)


def rebuild_indexes(target_engine):
    print("\n[indexes] Rebuilding text search GIN indexes on Article...")
    with target_engine.connect() as conn:
        conn.execute(text("SET statement_timeout = 0;"))
        print("  Building idx_article_title_trgm...")
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON "Article" USING gin (title gin_trgm_ops)'))
        print("  Building idx_article_abstract_trgm...")
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_abstract_trgm ON "Article" USING gin (abstract gin_trgm_ops)'))
        conn.commit()
    print("[indexes] GIN indexes rebuilt successfully.")


def migrate_table(src_engine, tgt_engine, table_name, resume=False):
    print(f"\n========================================================")
    print(f"[*] Processing Table: {table_name}")
    print(f"========================================================")

    with src_engine.connect() as src_conn, tgt_engine.connect() as tgt_conn:
        src_cols_info = get_columns(src_conn, table_name)
        tgt_cols_info = get_columns(tgt_conn, table_name)

        if not src_cols_info or not tgt_cols_info:
            print(f"[SKIP] Table '{table_name}' missing in source or target.")
            return

        common_cols = [c for c in src_cols_info if c in tgt_cols_info]
        print(f"  Columns mapped ({len(common_cols)}): {common_cols}")
        
        ignored_in_src = set(src_cols_info.keys()) - set(common_cols)
        if ignored_in_src:
            print(f"  Ignored source columns: {ignored_in_src}")

        total_src_rows = get_row_count(src_conn, table_name)
        tgt_start_rows = get_row_count(tgt_conn, table_name)
        print(f"  Source rows: {total_src_rows:,} | Target current rows: {tgt_start_rows:,}")

        if total_src_rows == 0:
            print(f"  Source table is empty. Skipping.")
            return

        if resume and tgt_start_rows == total_src_rows and total_src_rows > 0:
            print(f"  [SKIP] Target already has identical row count ({tgt_start_rows:,}).")
            return

    chunk_size = TABLE_CHUNK_SIZES.get(table_name, DEFAULT_CHUNK_SIZE)
    quoted_cols = ", ".join([f'"{c}"' for c in common_cols])
    json_cols = {c for c in common_cols if tgt_cols_info[c] in ("json", "jsonb")}

    existing_pks = set()
    pk_col = None
    if resume and tgt_start_rows > 0:
        with tgt_engine.connect() as tgt_conn:
            pks = get_pk_columns(tgt_conn, table_name)
            if len(pks) == 1 and pks[0] in common_cols and tgt_start_rows < 3000000:
                pk_col = pks[0]
                print(f"  [RESUME] Loading {tgt_start_rows:,} existing '{pk_col}' IDs from target to skip redundant transfer...")
                t_pk = time.time()
                existing_pks = set(r[0] for r in tgt_conn.execute(text(f'SELECT "{pk_col}" FROM "{table_name}"')))
                print(f"  [RESUME] Loaded {len(existing_pks):,} existing IDs in {time.time()-t_pk:.2f}s. Skipping them in source stream.")

    insert_query = f"""
        INSERT INTO "{table_name}" ({quoted_cols})
        VALUES %s
        ON CONFLICT DO NOTHING
    """

    select_sql = f'SELECT {quoted_cols} FROM "{table_name}"'

    t_start = time.time()
    migrated_rows = len(existing_pks)
    new_rows_processed = 0
    batch_num = 0
    pk_idx = common_cols.index(pk_col) if pk_col and existing_pks else None

    conn_ref = [get_fresh_tgt_conn(tgt_engine)]

    try:
        with src_engine.connect().execution_options(stream_results=True, yield_per=chunk_size) as src_conn:
            res = src_conn.execute(text(select_sql))
            batch = []

            for row in res:
                if pk_idx is not None and row[pk_idx] in existing_pks:
                    continue
                row_val = list(row)
                if json_cols:
                    for i, col in enumerate(common_cols):
                        if col in json_cols and row_val[i] is not None:
                            if isinstance(row_val[i], (dict, list)):
                                row_val[i] = Json(row_val[i])
                batch.append(tuple(row_val))

                if len(batch) >= chunk_size:
                    batch_num += 1
                    t0 = time.time()
                    execute_batch_with_retry(tgt_engine, conn_ref, insert_query, batch, max_retries=10)
                    t_batch = time.time() - t0
                    migrated_rows += len(batch)
                    new_rows_processed += len(batch)
                    pct = (migrated_rows / total_src_rows) * 100
                    elapsed = time.time() - t_start
                    rps = new_rows_processed / elapsed if elapsed > 0 else 0
                    remaining = total_src_rows - migrated_rows
                    eta_sec = remaining / rps if rps > 0 else 0
                    print(f"  -> Batch {batch_num:3d}: {migrated_rows:,}/{total_src_rows:,} ({pct:5.1f}%) | {len(batch)} rows in {t_batch:.2f}s | Speed: {rps:,.0f} r/s | ETA: {eta_sec:.0f}s")
                    batch = []

            if batch:
                batch_num += 1
                t0 = time.time()
                execute_batch_with_retry(tgt_engine, conn_ref, insert_query, batch, max_retries=10)
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
    with tgt_engine.connect() as tgt_conn:
        tgt_final_rows = get_row_count(tgt_conn, table_name)
    print(f"[OK] Completed '{table_name}' in {total_time:.1f}s. Target rows: {tgt_final_rows:,}")


def verify_all(src_engine, tgt_engine, tables=None):
    print("\n================================================================")
    print("                 FINAL AUDIT & VERIFICATION                     ")
    print("================================================================")
    print(f"{'Table Name':<35} {'Source Rows':>15} {'Target Rows':>15} {'Status':>10}")
    print("-" * 78)
    
    tables_to_check = tables if tables else TABLES_ORDER
    all_matched = True
    with src_engine.connect() as sc, tgt_engine.connect() as tc:
        for tbl in tables_to_check:
            s_cnt = get_row_count(sc, tbl)
            t_cnt = get_row_count(tc, tbl)
            status = "MATCH" if s_cnt == t_cnt else ("DIFF" if t_cnt > 0 else "EMPTY")
            if s_cnt != t_cnt:
                all_matched = False
            print(f"{tbl:<35} {s_cnt:>15,} {t_cnt:>15,} {status:>10}")
            
    print("=" * 78)
    if all_matched:
        print("[SUCCESS] ALL TABLES MATCH 100% WITH SOURCE DATABASE!")
    else:
        print("[NOTICE] Some differences noted (check above table).")


def run_migration(resume=False, clean=False, target_tables=None):
    print("================================================================")
    print("      RESEARCHPULSE DATA MIGRATION (OLD DB -> NEW DB)           ")
    print("================================================================")
    print(f"SOURCE: {OLD_URL}")
    print(f"TARGET: {NEW_URL}")
    print(f"MODE:   {'RESUME (Skip truncated tables)' if resume else 'CLEAN START'}")
    if target_tables:
        print(f"TABLES: {target_tables}")
    print("================================================================")

    src_engine = make_engine(OLD_URL)
    tgt_engine = make_engine(NEW_URL)

    with src_engine.connect() as c1, tgt_engine.connect() as c2:
        db1 = c1.execute(text("SELECT current_database()")).scalar()
        db2 = c2.execute(text("SELECT current_database()")).scalar()
        print(f"Source connected: {db1}")
        print(f"Target connected: {db2}")

    t_global_start = time.time()

    # Step 1: Prepare target DB
    if not resume and not target_tables:
        prepare_target_database(tgt_engine)
    else:
        print("\n[prepare] Ensuring GIN indexes are dropped before insert...")
        with tgt_engine.begin() as conn:
            conn.execute(text('DROP INDEX IF EXISTS idx_article_abstract_trgm'))
            conn.execute(text('DROP INDEX IF EXISTS idx_article_title_trgm'))
        print("  Dropped GIN trgm indexes.")

    # Step 2: Migrate tables
    tables_to_run = target_tables if target_tables else TABLES_ORDER
    for table in tables_to_run:
        migrate_table(src_engine, tgt_engine, table, resume=resume)

    # Step 3: Restore project relations
    if not target_tables or "Project" in target_tables or "Subject_Area" in target_tables:
        restore_project_relations(tgt_engine)

    # Step 4: Reset sequences
    reset_sequences(tgt_engine, tables=tables_to_run)

    # Step 5: Rebuild text search GIN indexes
    if not target_tables or "Article" in target_tables:
        rebuild_indexes(tgt_engine)

    # Step 6: Verify
    verify_all(src_engine, tgt_engine, tables=TABLES_ORDER)

    total_elapsed = time.time() - t_global_start
    print("\n================================================================")
    print(f"[FINISHED] Migration completed in {total_elapsed / 60:.2f} minutes!")
    print("================================================================")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Migrate complete academic data from Old DB to New DB")
    parser.add_argument("--resume", action="store_true", help="Resume migration without truncating target tables")
    parser.add_argument("--clean", action="store_true", help="Force clean migration (truncate target tables first)")
    parser.add_argument("--table", nargs="+", help="Specific table(s) to migrate")
    args = parser.parse_args()

    run_migration(resume=args.resume, clean=args.clean, target_tables=args.table)

