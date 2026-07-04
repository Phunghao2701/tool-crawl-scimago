"""
Init schema on Supabase then migrate data from Local PostgreSQL.

Run: python tools/migrate_local_to_vercel.py
"""
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import time
import argparse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, pool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env.local"), override=False)
LOCAL_URL = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")

load_dotenv(os.path.join(BASE_DIR, ".env.vercel"), override=True)
VERCEL_URL = os.getenv("VERCEL_DATABASE_URL") or os.getenv("DATABASE_URL")

if not LOCAL_URL or not VERCEL_URL or LOCAL_URL == VERCEL_URL:
    print("[ERROR] LOCAL_DATABASE_URL and VERCEL_DATABASE_URL must be different.")
    sys.exit(1)

# ─── Table order (FK: parents before children) ────────────────────────────────
TABLES_ORDER = [
    "Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric",
    "Journal", "Journal_Subject_Category",
    "Volume", "Issue",
    "Topic", "Article",
    "Author", "Author_Article",
    "Keyword", "Keyword_Article",
    "Sub_Topic",
    "Journal_Ranking", "Journal_Ranking_Subject_Category",
]

IDENTITY_TABLES = {
    "Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric",
    "Journal", "Volume", "Issue", "Topic", "Article", "Author",
    "Keyword", "Journal_Ranking",
}

# Tables where we UPSERT sync-status fields instead of skipping
UPSERT_UPDATE_COLS = {
    "Journal": ["source_id", "display_name", "issn",
                "publisher_id", "country", "region", "created_at", "is_deleted"],
}

# ─── Tuning ───────────────────────────────────────────────────────────────────
CHUNK_SIZE     = 1000   # rows per INSERT batch
COMMIT_EVERY   = 5000   # commit after this many rows

# ─── Sync Profiles ────────────────────────────────────────────────────────────
# Các bảng lớn và ước lượng số dòng hiện tại trên local
LARGE_TABLES = {
    "Article":        221_000,
    "Author":         577_000,
    "Author_Article": 1_282_000,
    "Keyword_Article":2_229_000,
    "Sub_Topic":       587_000,
    "Journal_Ranking": 328_000,
}

SYNC_PROFILES = {
    "1": {
        "name": "Journals only  (~50k rows, ~1 min)",
        "tables": [
            "Zone", "Subject_Area", "Subject_Category", "Publisher",
            "Ranking_Metric", "Journal", "Journal_Subject_Category",
            "Journal_Ranking", "Journal_Ranking_Subject_Category",
        ],
        "row_limits": {},
    },
    "2": {
        "name": "Journals + Articles  (~400k rows, ~10 min)",
        "tables": [
            "Zone", "Subject_Area", "Subject_Category", "Publisher",
            "Ranking_Metric", "Journal", "Journal_Subject_Category",
            "Volume", "Issue", "Topic", "Article",
            "Keyword", "Keyword_Article",
            "Journal_Ranking", "Journal_Ranking_Subject_Category",
        ],
        "row_limits": {},
    },
    "3": {
        "name": "Full  (~5M+ rows, 30-60 min)",
        "tables": None,   # None = all TABLES_ORDER
        "row_limits": {},
    },
    "4": {
        "name": "Custom  (chon so luong tung bang)",
        "tables": None,   # filled interactively
        "row_limits": {},
    },
}


# ─── Engine factory ───────────────────────────────────────────────────────────
def make_engine(url, poolsize=3):
    """Create engine with conservative pool to avoid connection limits."""
    return create_engine(
        url,
        poolclass=pool.QueuePool,
        pool_size=poolsize,
        max_overflow=1,
        pool_timeout=30,
        pool_recycle=600,
        connect_args={"connect_timeout": 20, "options": "-c statement_timeout=300000"},
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl
        ORDER BY ordinal_position
    """), {"tbl": table_name}).fetchall()
    return [r[0] for r in rows]


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


def truncate_all(conn):
    print("\n[truncate] Clearing Vercel DB...")
    for tbl in reversed(TABLES_ORDER):
        if table_exists(conn, tbl):
            conn.execute(text(f'TRUNCATE TABLE "{tbl}" RESTART IDENTITY CASCADE'))
    conn.commit()
    print("[truncate] Done.")


# ─── Schema init on fresh Supabase ────────────────────────────────────────────
def init_schema_if_empty(vercel_engine, schema_path):
    with vercel_engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"
        )).scalar()

    if total > 0:
        print(f"[schema] Vercel already has {total} tables, skipping schema init.")
        return

    print("[schema] Vercel DB is empty. Creating schema from schema.sql...")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()

    # Execute schema block by block (split on ; but keep body)
    with vercel_engine.begin() as conn:
        # Run entire schema as one shot — PostgreSQL handles it
        conn.execute(text(sql))
    print("[schema] Schema created successfully.")


# ─── Core: migrate one table ──────────────────────────────────────────────────
def build_insert_sql(table_name, cols):
    col_list   = ", ".join(f'"{c}"' for c in cols)
    param_list = ", ".join(f":{c}" for c in cols)
    prefix     = "OVERRIDING SYSTEM VALUE " if table_name in IDENTITY_TABLES else ""

    if table_name in UPSERT_UPDATE_COLS:
        update_cols = [c for c in UPSERT_UPDATE_COLS[table_name] if c in cols]
        if update_cols:
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            pk_col     = cols[0]
            conflict   = f'ON CONFLICT ("{pk_col}") DO UPDATE SET {set_clause}'
        else:
            conflict = "ON CONFLICT DO NOTHING"
    else:
        conflict = "ON CONFLICT DO NOTHING"

    return f'INSERT INTO "{table_name}" ({col_list}) {prefix}VALUES ({param_list}) {conflict}'


def migrate_table(local_conn, vercel_conn, table_name, mode, row_limit=None):
    """row_limit=None means copy ALL rows; row_limit=N means copy at most N rows."""
    t0 = time.time()

    if not table_exists(local_conn, table_name):
        print(f"  [{table_name}] Not in local, skip.")
        return

    local_total  = count_rows(local_conn, table_name)
    vercel_total = count_rows(vercel_conn, table_name)

    if local_total == 0:
        print(f"  [{table_name}] Empty locally, skip.")
        return

    # Effective cap: min(local rows, row_limit if set)
    effective_total = min(local_total, row_limit) if row_limit else local_total

    if mode == "incremental" and vercel_total >= effective_total and table_name not in UPSERT_UPDATE_COLS:
        print(f"  [{table_name}] Already up-to-date ({vercel_total:,} rows), skip.")
        return

    # Column intersection (safety for schema differences)
    local_cols  = get_columns(local_conn, table_name)
    vercel_cols = set(get_columns(vercel_conn, table_name))
    cols        = [c for c in local_cols if c in vercel_cols]

    if not cols:
        print(f"  [{table_name}] No matching columns, skip.")
        return

    insert_sql = build_insert_sql(table_name, cols)
    col_list   = ", ".join(f'"{c}"' for c in cols)

    offset    = 0
    processed = 0
    limit_tag = f" [limit={row_limit:,}]" if row_limit else ""

    print(f"  [{table_name}]{limit_tag} Fetching columns...", flush=True)

    while True:
        # Respect row_limit: don't fetch more than needed
        fetch = min(CHUNK_SIZE, effective_total - offset) if row_limit else CHUNK_SIZE
        if fetch <= 0:
            break

        rows = local_conn.execute(text(
            f'SELECT {col_list} FROM "{table_name}" ORDER BY 1 '
            f'LIMIT :lim OFFSET :off'
        ), {"lim": fetch, "off": offset}).fetchall()

        if not rows:
            break

        batch = [dict(zip(cols, row)) for row in rows]
        vercel_conn.execute(text(insert_sql), batch)
        processed += len(rows)

        # Commit every COMMIT_EVERY rows instead of every chunk
        if processed % COMMIT_EVERY == 0 or len(rows) < fetch:
            vercel_conn.commit()

        elapsed = time.time() - t0
        pct     = processed / effective_total * 100
        print(f"  [{table_name}]{limit_tag} {processed:,}/{effective_total:,} ({pct:.0f}%) {elapsed:.1f}s", end="\r", flush=True)
        offset += len(rows)  # use actual rows fetched

    vercel_conn.commit()
    vercel_after = count_rows(vercel_conn, table_name)
    elapsed      = time.time() - t0
    limit_tag    = f" [capped at {row_limit:,}]" if row_limit else ""
    print(f"  [{table_name}] OK  local={local_total:,}{limit_tag}  remote={vercel_after:,}  {elapsed:.1f}s      ", flush=True)

# ─── Interactive profile selector ────────────────────────────────────────────
def select_profile(local_engine):
    """
    Show profile menu, return (selected_tables, row_limits) tuple.
    selected_tables = list of table names to migrate
    row_limits      = dict {table_name: max_rows}  (empty = no limit)
    """
    print()
    print("  Chon pham vi dong bo (sync scope):")
    print("  " + "-" * 50)
    for key, p in SYNC_PROFILES.items():
        print(f"  {key}. {p['name']}")
    print("  " + "-" * 50)

    while True:
        choice = input("  Chon (1/2/3/4): ").strip()
        if choice in SYNC_PROFILES:
            break
        print("  Lua chon khong hop le, thu lai.")

    profile = SYNC_PROFILES[choice]

    if choice == "4":
        # Custom: let user set row limit per large table
        selected_tables = list(TABLES_ORDER)  # start with all
        row_limits = {}
        print()
        print("  [Custom] Nhap gioi han so dong cho cac bang lon (Enter = khong gioi han):")
        with local_engine.connect() as lc:
            for tbl in selected_tables:
                if tbl in LARGE_TABLES:
                    local_cnt = count_rows(lc, tbl)
                    val = input(f"  {tbl} ({local_cnt:,} rows) - Gioi han (Enter=all): ").strip()
                    if val.isdigit() and int(val) > 0:
                        row_limits[tbl] = int(val)
        return selected_tables, row_limits

    selected_tables = profile["tables"] if profile["tables"] is not None else list(TABLES_ORDER)
    return selected_tables, profile["row_limits"]


# ─── Main ─────────────────────────────────────────────────────────────────────
def run_migration(mode="incremental"):
    print("=" * 62)
    label = "FULL RESET" if mode == "reset" else "INCREMENTAL"
    print(f"  LOCAL -> SUPABASE MIGRATION  [{label}]")
    print("=" * 62)
    print(f"  Local : {LOCAL_URL[:55]}...")
    print(f"  Remote: {VERCEL_URL[:55]}...")
    print()

    local_engine  = make_engine(LOCAL_URL,  poolsize=2)
    vercel_engine = make_engine(VERCEL_URL, poolsize=2)

    # Test connections
    print("[check] Local  DB... ", end="")
    with local_engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar()
        print(f"{db} [OK]")

    print("[check] Supabase DB... ", end="")
    with vercel_engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar()
        print(f"{db} [OK]")

    # Init schema if Supabase is empty
    schema_path = os.path.join(BASE_DIR, "database", "schema.sql")
    init_schema_if_empty(vercel_engine, schema_path)

    # ── Profile selection ─────────────────────────────────────────────────────
    selected_tables, row_limits = select_profile(local_engine)
    print()
    print(f"  Tables selected: {len(selected_tables)}")
    if row_limits:
        print(f"  Row limits applied: {row_limits}")

    # Show row-count comparison (only for selected tables)
    print()
    print(f"  {'Table':<38} {'Local':>9} {'Remote':>9}  {'Limit':>9}  Status")
    print(f"  {'-'*38} {'-'*9} {'-'*9}  {'-'*9}  {'-'*16}")
    with local_engine.connect() as lc, vercel_engine.connect() as vc:
        for tbl in selected_tables:
            lc_ = count_rows(lc, tbl)
            vc_ = count_rows(vc, tbl)
            lim = row_limits.get(tbl)
            effective = min(lc_, lim) if lim else lc_
            if lc_ == 0 and vc_ == 0:
                continue
            if vc_ >= effective and tbl not in UPSERT_UPDATE_COLS:
                status = "up-to-date"
            elif vc_ == 0:
                status = "needs copy"
            else:
                status = f"missing {effective - vc_:,}"
            lim_str = f"{lim:,}" if lim else "-"
            print(f"  {tbl:<38} {lc_:>9,} {vc_:>9,}  {lim_str:>9}  {status}")

    print()
    warn = "ALL Supabase data will be DELETED first. " if mode == "reset" else ""
    ans  = input(f"Proceed? {warn}(y/N): ").strip().lower()
    if ans != "y":
        print("Cancelled.")
        return

    t_start = time.time()

    if mode == "reset":
        with vercel_engine.connect() as vc:
            try:
                vc.execute(text("SET session_replication_role = replica;"))
                vc.commit()
            except Exception:
                vc.rollback()
            truncate_all(vc)

    print("\n[migrate] Copying data...")
    with local_engine.connect() as lc, vercel_engine.connect() as vc:
        # Try to disable FK checks for speed (may not work on proxied connections like Prisma)
        try:
            vc.execute(text("SET session_replication_role = replica;"))
            vc.commit()
            print("[info] FK checks disabled (faster inserts).")
        except Exception:
            print("[info] FK checks not disabled (Prisma proxy). Continuing with FK on.")
            vc.rollback()

        for tbl in selected_tables:
            print(f"  [{tbl}] Starting...", flush=True)
            migrate_table(lc, vc, tbl, mode, row_limit=row_limits.get(tbl))

        try:
            vc.execute(text("SET session_replication_role = DEFAULT;"))
            vc.commit()
        except Exception:
            pass

    elapsed = time.time() - t_start
    print(f"\n[OK] Done in {elapsed:.1f}s!")

    print("\n[verify] Final counts on Supabase:")
    with vercel_engine.connect() as vc:
        for tbl in selected_tables:
            cnt = count_rows(vc, tbl)
            if cnt > 0:
                print(f"  {tbl}: {cnt:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Local -> Supabase")
    parser.add_argument("--reset", action="store_true",
                        help="Full reset: truncate Supabase, then copy everything.")
    args = parser.parse_args()
    run_migration("reset" if args.reset else "incremental")
