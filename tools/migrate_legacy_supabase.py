"""
One-time sync: LEGACY Supabase (egyrzaqtmxmcezxchfrl) -> current production DB
(VERCEL_DATABASE_URL in .env.vercel). Idempotent: safe to re-run, skips tables
already up-to-date on the target.

Run: python tools/migrate_legacy_supabase.py
"""
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from sqlalchemy import create_engine, text, pool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env.vercel"), override=True)
OLD_URL = os.getenv("LEGACY_SUPABASE_DATABASE_URL")
NEW_URL = os.getenv("VERCEL_DATABASE_URL")

if not OLD_URL or not NEW_URL:
    print("[ERROR] LEGACY_SUPABASE_DATABASE_URL and VERCEL_DATABASE_URL must be set in .env.vercel.")
    sys.exit(1)

# ─── Table order (FK: parents before children) ──────────────────────────────
TABLES_ORDER = [
    # Standalone / root tables
    "user",
    "Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric",
    "coin_package",
    # Tables depending on root
    "Journal", "Journal_Subject_Category",
    "Volume", "Issue",
    "Topic", "Article",
    "Author", "Author_Article",
    "Keyword", "Keyword_Article",
    "Sub_Topic",
    "Institution", "Institution_Author",
    "Journal_Ranking", "Journal_Ranking_Subject_Category",
    # User-dependent tables
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
    "system_log",
]

# Tables with IDENTITY columns (need OVERRIDING SYSTEM VALUE)
IDENTITY_TABLES = {
    "Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric",
    "Journal", "Volume", "Issue", "Topic", "Article", "Author",
    "Keyword", "Journal_Ranking",
    "Institution",
    "Project", "Project_Member",
    "Project_Chat_Message", "System_Log",
}

# Old DB table name -> new DB table name, where casing differs between the two.
NAME_MAP = {
    "system_log": "System_Log",
}

# Tuning
CHUNK_SIZE = 500
COMMIT_EVERY = 2000

SCHEMA_PATH = os.path.join(BASE_DIR, "Scientific_Journal_Publication_Trend_Tracking_System_FIXED.sql")


def make_engine(url, poolsize=2):
    return create_engine(
        url,
        poolclass=pool.QueuePool,
        pool_size=poolsize,
        max_overflow=1,
        pool_timeout=30,
        pool_recycle=600,
        connect_args={"connect_timeout": 20, "options": "-c statement_timeout=600000"},
    )


def get_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl
        ORDER BY ordinal_position
    """), {"tbl": table_name}).fetchall()
    return [r[0] for r in rows]


def get_json_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl
          AND data_type IN ('json', 'jsonb')
    """), {"tbl": table_name}).fetchall()
    return {r[0] for r in rows}


def table_exists(conn, table_name):
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=:tbl"
    ), {"tbl": table_name}).fetchone()
    return row is not None


def count_rows(conn, table_name):
    if not table_exists(conn, table_name):
        return 0
    return conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()


def init_schema(engine, schema_path):
    """Create schema on new DB if empty."""
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"
        )).scalar()

    if total > 0:
        print(f"[schema] Target DB already has {total} tables, skipping schema init.")
        return

    print(f"[schema] Target DB is empty. Creating schema from {os.path.basename(schema_path)}...")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()

    with engine.begin() as conn:
        conn.execute(text(sql))
    print("[schema] Schema created successfully.")


def build_insert_sql(table_name, cols):
    col_list = ", ".join(f'"{c}"' for c in cols)
    param_list = ", ".join(f":{c}" for c in cols)
    prefix = "OVERRIDING SYSTEM VALUE " if table_name in IDENTITY_TABLES else ""
    return f'INSERT INTO "{table_name}" ({col_list}) {prefix}VALUES ({param_list}) ON CONFLICT DO NOTHING'


def migrate_project_member(old_conn, new_conn):
    """Project_Member.invited_email is NOT NULL in the new schema but doesn't
    exist in the legacy schema; backfill it from user.email via user_id."""
    old_total = count_rows(old_conn, "Project_Member")
    new_total = count_rows(new_conn, "Project_Member")
    if old_total == 0 or new_total >= old_total:
        print(f"  [Project_Member] Already up-to-date ({new_total:,} rows), skip.")
        return

    rows = old_conn.execute(text("""
        SELECT pm.project_member_id, pm.project_id, pm.user_id,
               COALESCE(u.email, '') AS invited_email,
               pm.role, pm.status, pm.invited_by, pm.invited_at, pm.accepted_at, pm.removed_at
        FROM "Project_Member" pm
        LEFT JOIN "user" u ON u.user_id = pm.user_id
        ORDER BY pm.project_member_id
    """)).mappings().fetchall()

    batch = [dict(r) for r in rows]
    insert_sql = build_insert_sql("Project_Member", list(batch[0].keys()))
    new_conn.execute(text(insert_sql), batch)
    new_conn.commit()
    new_after = count_rows(new_conn, "Project_Member")
    print(f"  [Project_Member] OK  old={old_total:,}  new={new_after:,}")


def migrate_table(old_conn, new_conn, table_name):
    if table_name == "Project_Member":
        return migrate_project_member(old_conn, new_conn)

    t0 = time.time()
    new_table_name = NAME_MAP.get(table_name, table_name)

    if not table_exists(old_conn, table_name):
        print(f"  [{table_name}] Not in legacy DB, skip.")
        return

    old_total = count_rows(old_conn, table_name)
    new_total = count_rows(new_conn, new_table_name)

    if old_total == 0:
        print(f"  [{table_name}] Empty in legacy DB, skip.")
        return

    if new_total >= old_total:
        print(f"  [{table_name}] Already up-to-date ({new_total:,} rows), skip.")
        return

    # Column intersection
    old_cols = get_columns(old_conn, table_name)
    new_cols_set = set(get_columns(new_conn, new_table_name))
    cols = [c for c in old_cols if c in new_cols_set]

    if not cols:
        print(f"  [{table_name}] No matching columns, skip.")
        return

    json_cols = get_json_columns(new_conn, new_table_name) & set(cols)

    insert_sql = build_insert_sql(new_table_name, cols)
    col_list = ", ".join(f'"{c}"' for c in cols)

    offset = 0
    processed = 0

    while True:
        rows = old_conn.execute(text(
            f'SELECT {col_list} FROM "{table_name}" ORDER BY 1 '
            f'LIMIT :lim OFFSET :off'
        ), {"lim": CHUNK_SIZE, "off": offset}).fetchall()

        if not rows:
            break

        batch = []
        for row in rows:
            record = dict(zip(cols, row))
            for jc in json_cols:
                if record[jc] is not None and not isinstance(record[jc], str):
                    record[jc] = json.dumps(record[jc])
            batch.append(record)
        new_conn.execute(text(insert_sql), batch)
        processed += len(rows)

        if processed % COMMIT_EVERY == 0 or len(rows) < CHUNK_SIZE:
            new_conn.commit()

        elapsed = time.time() - t0
        pct = processed / old_total * 100
        print(f"  [{table_name}] {processed:,}/{old_total:,} ({pct:.0f}%) {elapsed:.1f}s", end="\r", flush=True)
        offset += len(rows)

    new_conn.commit()
    new_after = count_rows(new_conn, new_table_name)
    elapsed = time.time() - t0
    print(f"  [{table_name}] OK  old={old_total:,}  new={new_after:,}  {elapsed:.1f}s" + " " * 20)


def run():
    print("=" * 62)
    print("  LEGACY SUPABASE -> PRODUCTION DB SYNC")
    print("=" * 62)
    print(f"  Source: LEGACY_SUPABASE_DATABASE_URL (.env.vercel)")
    print(f"  Target: VERCEL_DATABASE_URL (.env.vercel)")
    print()

    old_engine = make_engine(OLD_URL, poolsize=2)
    new_engine = make_engine(NEW_URL, poolsize=2)

    # Test connections
    print("[check] Legacy Supabase... ", end="", flush=True)
    with old_engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar()
        print(f"{db} [OK]")

    print("[check] Production DB... ", end="", flush=True)
    with new_engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar()
        print(f"{db} [OK]")

    # Init schema
    init_schema(new_engine, SCHEMA_PATH)

    # Show comparison
    print()
    print(f"  {'Table':<38} {'Legacy':>9} {'Prod':>9}  Status")
    print(f"  {'-'*38} {'-'*9} {'-'*9}  {'-'*16}")
    with old_engine.connect() as oc, new_engine.connect() as nc:
        for tbl in TABLES_ORDER:
            oc_ = count_rows(oc, tbl)
            nc_ = count_rows(nc, NAME_MAP.get(tbl, tbl))
            if oc_ == 0 and nc_ == 0:
                continue
            if nc_ >= oc_:
                status = "up-to-date"
            elif nc_ == 0:
                status = "needs copy"
            else:
                status = f"missing {oc_ - nc_:,}"
            print(f"  {tbl:<38} {oc_:>9,} {nc_:>9,}  {status}")

    print()
    ans = input("Proceed with migration? (y/N): ").strip().lower()
    if ans != "y":
        print("Cancelled.")
        return

    t_start = time.time()
    print("\n[migrate] Copying data...")

    with old_engine.connect() as oc, new_engine.connect() as nc:
        # Disable FK checks for speed
        try:
            nc.execute(text("SET session_replication_role = replica;"))
            nc.commit()
            print("[info] FK checks disabled (faster inserts).")
        except Exception:
            print("[info] Could not disable FK checks. Continuing with FK on.")
            nc.rollback()

        for tbl in TABLES_ORDER:
            migrate_table(oc, nc, tbl)

        try:
            nc.execute(text("SET session_replication_role = DEFAULT;"))
            nc.commit()
        except Exception:
            pass

    elapsed = time.time() - t_start
    print(f"\n[OK] Done in {elapsed:.1f}s!")

    # Verify
    print("\n[verify] Final counts on production DB:")
    with new_engine.connect() as nc:
        for tbl in TABLES_ORDER:
            cnt = count_rows(nc, NAME_MAP.get(tbl, tbl))
            if cnt > 0:
                print(f"  {tbl}: {cnt:,}")


if __name__ == "__main__":
    run()
