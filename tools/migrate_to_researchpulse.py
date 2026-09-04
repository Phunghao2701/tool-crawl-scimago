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
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

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


def reset_sequences(target_engine):
    print("\n[sequences] Resetting all table sequences in target DB...")
    with target_engine.begin() as conn:
        for tbl in TABLES_ORDER:
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


def rebuild_indexes(target_engine):
    print("\n[indexes] Rebuilding text search GIN indexes on Article...")
    with target_engine.begin() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON "Article" USING gin (title gin_trgm_ops)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_article_abstract_trgm ON "Article" USING gin (abstract gin_trgm_ops)'))
    print("[indexes] GIN indexes rebuilt successfully.")


def migrate_table(src_engine, tgt_engine, table_name):
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

    chunk_size = TABLE_CHUNK_SIZES.get(table_name, DEFAULT_CHUNK_SIZE)
    quoted_cols = ", ".join([f'"{c}"' for c in common_cols])
    json_cols = {c for c in common_cols if tgt_cols_info[c] in ("json", "jsonb")}

    insert_query = f"""
        INSERT INTO "{table_name}" ({quoted_cols})
        VALUES %s
        ON CONFLICT DO NOTHING
    """

    select_sql = f'SELECT {quoted_cols} FROM "{table_name}"'

    t_start = time.time()
    migrated_rows = 0
    batch_num = 0

    tgt_raw_conn = tgt_engine.raw_connection()
    tgt_raw_conn.autocommit = False

    try:
        with src_engine.connect().execution_options(stream_results=True, yield_per=chunk_size) as src_conn:
            res = src_conn.execute(text(select_sql))
            batch = []

            for row in res:
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
                    with tgt_raw_conn.cursor() as cur:
                        execute_values(cur, insert_query, batch, page_size=len(batch))
                    tgt_raw_conn.commit()
                    t_batch = time.time() - t0
                    migrated_rows += len(batch)
                    pct = (migrated_rows / total_src_rows) * 100
                    elapsed = time.time() - t_start
                    rps = migrated_rows / elapsed if elapsed > 0 else 0
                    remaining = total_src_rows - migrated_rows
                    eta_sec = remaining / rps if rps > 0 else 0
                    print(f"  -> Batch {batch_num:3d}: {migrated_rows:,}/{total_src_rows:,} ({pct:5.1f}%) | {len(batch)} rows in {t_batch:.2f}s | Speed: {rps:,.0f} r/s | ETA: {eta_sec:.0f}s")
                    batch = []

            if batch:
                batch_num += 1
                t0 = time.time()
                with tgt_raw_conn.cursor() as cur:
                    execute_values(cur, insert_query, batch, page_size=len(batch))
                tgt_raw_conn.commit()
                t_batch = time.time() - t0
                migrated_rows += len(batch)
                print(f"  -> Batch {batch_num:3d} (Final): {migrated_rows:,}/{total_src_rows:,} (100.0%) in {t_batch:.2f}s")

    finally:
        tgt_raw_conn.close()

    total_time = time.time() - t_start
    with tgt_engine.connect() as tgt_conn:
        tgt_final_rows = get_row_count(tgt_conn, table_name)
    print(f"[OK] Completed '{table_name}' in {total_time:.1f}s. Target rows: {tgt_final_rows:,}")


def verify_all(src_engine, tgt_engine):
    print("\n================================================================")
    print("                 FINAL AUDIT & VERIFICATION                     ")
    print("================================================================")
    print(f"{'Table Name':<35} {'Source Rows':>15} {'Target Rows':>15} {'Status':>10}")
    print("-" * 78)
    
    all_matched = True
    with src_engine.connect() as sc, tgt_engine.connect() as tc:
        for tbl in TABLES_ORDER:
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


def run_migration():
    print("================================================================")
    print("      RESEARCHPULSE DATA MIGRATION (OLD DB -> NEW DB)           ")
    print("================================================================")
    print(f"SOURCE: {OLD_URL}")
    print(f"TARGET: {NEW_URL}")
    print("================================================================")

    src_engine = make_engine(OLD_URL)
    tgt_engine = make_engine(NEW_URL)

    with src_engine.connect() as c1, tgt_engine.connect() as c2:
        db1 = c1.execute(text("SELECT current_database()")).scalar()
        db2 = c2.execute(text("SELECT current_database()")).scalar()
        print(f"Source connected: {db1}")
        print(f"Target connected: {db2}")

    t_global_start = time.time()

    # Step 1: Prepare target DB (clean test dummy data, keep user & project)
    prepare_target_database(tgt_engine)

    # Step 2: Migrate all 18 tables
    for table in TABLES_ORDER:
        migrate_table(src_engine, tgt_engine, table)

    # Step 3: Restore project relations
    restore_project_relations(tgt_engine)

    # Step 4: Reset sequences
    reset_sequences(tgt_engine)

    # Step 5: Rebuild text search GIN indexes
    rebuild_indexes(tgt_engine)

    # Step 6: Verify
    verify_all(src_engine, tgt_engine)

    total_elapsed = time.time() - t_global_start
    print("\n================================================================")
    print(f"[FINISHED] Migration completed in {total_elapsed / 60:.2f} minutes!")
    print("================================================================")


if __name__ == "__main__":
    run_migration()
