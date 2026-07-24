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

import json
import time
import argparse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, pool
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError as SAOperationalError
from psycopg2 import InterfaceError as PsycopgInterfaceError
from psycopg2 import OperationalError as PsycopgOperationalError
from psycopg2.extras import execute_values

from pipeline_lock import acquire as acquire_lock

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
CHUNK_SIZE          = 1000   # wide/identity rows per batch
LINK_CHUNK_SIZE    = 10000  # narrow composite-link rows per batch
VALUES_PAGE_SIZE   = 2000   # rows per multi-value INSERT statement
MAX_BATCH_RETRIES  = 3      # bounded retry for transient connection loss
RETRY_BASE_SECONDS = 2

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
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 20,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
            "options": "-c statement_timeout=300000",
        },
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_columns(conn, table_name):
    rows = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl
        ORDER BY ordinal_position
    """), {"tbl": table_name}).fetchall()
    return [r[0] for r in rows]


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


def get_active_article_manifest(vercel_engine):
    """Return active manifest metadata, or None when manifesting is unused."""
    with vercel_engine.connect() as conn:
        manifest_table = conn.execute(text(
            "SELECT to_regclass('pipeline.article_manifests')"
        )).scalar()
        if manifest_table is None:
            return None
        row = conn.execute(text("""
            SELECT manifest_name, selected_count, selection_checksum
            FROM pipeline.article_manifests
            WHERE is_active
        """)).mappings().first()
        return dict(row) if row else None


def enforce_article_manifest_guard(vercel_engine):
    """Block generic migration while a curated production manifest is active."""
    manifest = get_active_article_manifest(vercel_engine)
    if manifest is None:
        return
    print()
    print(
        f"[MANIFEST] Active: {manifest['manifest_name']} "
        f"({manifest['selected_count']:,} Article rows)."
    )
    print(
        "[MANIFEST] Generic local -> production migration is locked to "
        "prevent restoring data outside the curated manifest."
    )
    print(
        "[MANIFEST] Rebuild or deactivate the manifest explicitly before "
        "running a general migration."
    )
    raise SystemExit(1)


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
def build_conflict_clause(table_name, cols):
    if table_name in UPSERT_UPDATE_COLS:
        update_cols = [c for c in UPSERT_UPDATE_COLS[table_name] if c in cols]
        if update_cols:
            set_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            pk_col     = cols[0]
            distinct_clause = " OR ".join(
                f'"{table_name}"."{c}" IS DISTINCT FROM EXCLUDED."{c}"'
                for c in update_cols
            )
            return (
                f'ON CONFLICT ("{pk_col}") DO UPDATE SET {set_clause} '
                f'WHERE {distinct_clause}'
            )
        else:
            return "ON CONFLICT DO NOTHING"
    return "ON CONFLICT DO NOTHING"


def build_insert_sql(table_name, cols):
    """Legacy named-parameter form retained for compatibility/tests."""
    col_list   = ", ".join(f'"{c}"' for c in cols)
    param_list = ", ".join(f":{c}" for c in cols)
    prefix     = "OVERRIDING SYSTEM VALUE " if table_name in IDENTITY_TABLES else ""
    conflict   = build_conflict_clause(table_name, cols)

    return f'INSERT INTO "{table_name}" ({col_list}) {prefix}VALUES ({param_list}) {conflict}'


def build_values_insert_sql(table_name, cols):
    """psycopg2 execute_values form: one SQL round trip per VALUES page."""
    col_list = ", ".join(f'"{c}"' for c in cols)
    prefix = "OVERRIDING SYSTEM VALUE " if table_name in IDENTITY_TABLES else ""
    conflict = build_conflict_clause(table_name, cols)
    return f'INSERT INTO "{table_name}" ({col_list}) {prefix}VALUES %s {conflict}'


def prepare_values(rows, cols, json_cols):
    json_indexes = {cols.index(c) for c in json_cols}
    values = []
    for row in rows:
        record = list(row)
        for idx in json_indexes:
            if record[idx] is not None and not isinstance(record[idx], str):
                record[idx] = json.dumps(record[idx], ensure_ascii=False)
        values.append(tuple(record))
    return values


def build_page_query(table_name, cols, pk_cols, cursor=None, initial_offset=0):
    col_list = ", ".join(f'"{c}"' for c in cols)
    order_cols = pk_cols or [cols[0]]
    order_sql = ", ".join(f'"{c}"' for c in order_cols)
    params = {}

    if cursor is not None and pk_cols:
        left = ", ".join(f'"{c}"' for c in pk_cols)
        right = ", ".join(f":cursor_{idx}" for idx in range(len(pk_cols)))
        where_sql = f"WHERE ({left}) > ({right})"
        params.update({f"cursor_{idx}": value for idx, value in enumerate(cursor)})
        offset_sql = ""
    else:
        where_sql = ""
        offset_sql = " OFFSET :off" if initial_offset else ""
        if initial_offset:
            params["off"] = initial_offset

    query = (
        f'SELECT {col_list} FROM "{table_name}" {where_sql} '
        f'ORDER BY {order_sql} LIMIT :lim{offset_sql}'
    )
    return query, params


def get_remote_update_signatures(conn, table_name, cols):
    update_cols = [c for c in UPSERT_UPDATE_COLS.get(table_name, []) if c in cols]
    if not update_cols:
        return None
    pk_col = cols[0]
    select_cols = ", ".join([f'"{pk_col}"'] + [f'"{c}"' for c in update_cols])
    rows = conn.execute(text(f'SELECT {select_cols} FROM "{table_name}"')).fetchall()
    return {
        row[0]: tuple(row[1:])
        for row in rows
    }


def filter_unchanged_upserts(table_name, cols, values, remote_signatures):
    if remote_signatures is None:
        return values, 0
    update_cols = [c for c in UPSERT_UPDATE_COLS.get(table_name, []) if c in cols]
    pk_idx = 0
    update_indexes = [cols.index(c) for c in update_cols]
    changed = []
    skipped = 0
    for value in values:
        local_signature = tuple(value[idx] for idx in update_indexes)
        if remote_signatures.get(value[pk_idx]) == local_signature:
            skipped += 1
        else:
            changed.append(value)
    return changed, skipped


def execute_values_batch(vercel_engine, insert_sql, values, disable_fk_checks):
    if not values:
        return

    for attempt in range(1, MAX_BATCH_RETRIES + 1):
        conn = None
        transaction = None
        try:
            conn = vercel_engine.connect()
            transaction = conn.begin()
            if disable_fk_checks:
                conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
            raw_conn = conn.connection.driver_connection
            with raw_conn.cursor() as cursor:
                execute_values(
                    cursor,
                    insert_sql,
                    values,
                    page_size=VALUES_PAGE_SIZE,
                )
            transaction.commit()
            return
        except (PsycopgOperationalError, PsycopgInterfaceError, SAOperationalError) as exc:
            if transaction is not None and transaction.is_active:
                try:
                    transaction.rollback()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.invalidate()
                except Exception:
                    pass
            if attempt >= MAX_BATCH_RETRIES:
                raise
            delay = RETRY_BASE_SECONDS * attempt
            print(
                f"  [WARN] Remote connection lost; retrying committed-safe batch "
                f"{attempt}/{MAX_BATCH_RETRIES - 1} in {delay}s: {exc}",
                flush=True,
            )
            time.sleep(delay)
        except Exception:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()


def supports_replica_role(vercel_engine):
    try:
        with vercel_engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        return True
    except Exception:
        return False


def migrate_table(
    local_conn,
    vercel_engine,
    table_name,
    mode,
    row_limit=None,
    disable_fk_checks=False,
):
    """row_limit=None means copy ALL rows; row_limit=N means copy at most N rows."""
    t0 = time.time()

    if not table_exists(local_conn, table_name):
        print(f"  [{table_name}] Not in local, skip.")
        return

    local_total = count_rows(local_conn, table_name)
    with vercel_engine.connect() as vercel_conn:
        vercel_total = count_rows(vercel_conn, table_name)

    if local_total == 0:
        print(f"  [{table_name}] Empty locally, skip.")
        return

    # Effective cap: min(local rows, row_limit if set)
    effective_total = min(local_total, row_limit) if row_limit else local_total

    if mode == "incremental" and vercel_total >= effective_total and table_name not in UPSERT_UPDATE_COLS:
        print(f"  [{table_name}] Already up-to-date ({vercel_total:,} rows), skip.")
        return

    # Column intersection (safety for schema differences) and stable PK order.
    local_cols = get_columns(local_conn, table_name)
    pk_cols = get_pk_columns(local_conn, table_name)
    with vercel_engine.connect() as vercel_conn:
        vercel_cols = set(get_columns(vercel_conn, table_name))
        cols = [c for c in local_cols if c in vercel_cols]
        json_cols = get_json_columns(vercel_conn, table_name) & set(cols)
        remote_signatures = get_remote_update_signatures(
            vercel_conn,
            table_name,
            cols,
        )

    if not cols:
        print(f"  [{table_name}] No matching columns, skip.")
        return
    pk_cols = [c for c in pk_cols if c in cols]

    insert_sql = build_values_insert_sql(table_name, cols)

    # Resume near where we left off instead of re-reading+re-sending every row
    # from OFFSET 0 every run. Only safe when `ORDER BY 1` is a single
    # surrogate auto-increment PK (IDENTITY_TABLES): that column is unique per
    # row, so "the first `vercel_total` rows in that order" is a stable,
    # well-defined prefix.
    #
    # It is NOT safe for join/link tables (Author_Article, Keyword_Article,
    # Sub_Topic, ...): their first ORDER BY column (e.g. author_id) repeats
    # across many rows, so "offset N" is not a fixed set of rows. Worse, when
    # a later crawl adds NEW links for an ALREADY-synced parent row, those new
    # rows land interleaved in the middle of that ORDER BY, not at the tail -
    # so resuming from `vercel_total` silently skips them forever (confirmed:
    # local rows at offset 900k already existed remotely while remote total
    # was only 872k, i.e. the real gap was earlier in the scan, not at the
    # end). These tables always rescan from 0; ON CONFLICT DO NOTHING keeps
    # that cheap for rows already present.
    RESUME_SAFETY_MARGIN = 2000
    if (
        mode == "incremental"
        and table_name in IDENTITY_TABLES
        and table_name not in UPSERT_UPDATE_COLS
        and not row_limit
    ):
        offset = max(0, vercel_total - RESUME_SAFETY_MARGIN)
    else:
        offset = 0
    processed = offset
    fallback_offset = offset
    cursor = None
    pk_indexes = [cols.index(c) for c in pk_cols]
    chunk_size = CHUNK_SIZE if table_name in IDENTITY_TABLES else LINK_CHUNK_SIZE
    sent_rows = 0
    unchanged_rows = 0
    limit_tag = f" [limit={row_limit:,}]" if row_limit else ""
    if offset:
        print(f"  [{table_name}]{limit_tag} Resuming from offset {offset:,} (remote already has {vercel_total:,}).", flush=True)

    print(f"  [{table_name}]{limit_tag} Fetching columns...", flush=True)

    while True:
        remaining = effective_total - processed
        fetch = min(chunk_size, remaining)
        if fetch <= 0:
            break

        query, params = build_page_query(
            table_name,
            cols,
            pk_cols,
            cursor=cursor,
            initial_offset=fallback_offset if cursor is None else 0,
        )
        params["lim"] = fetch
        rows = local_conn.execute(text(query), params).fetchall()

        if not rows:
            break

        values = prepare_values(rows, cols, json_cols)
        values, skipped = filter_unchanged_upserts(
            table_name,
            cols,
            values,
            remote_signatures,
        )
        execute_values_batch(
            vercel_engine,
            insert_sql,
            values,
            disable_fk_checks,
        )
        sent_rows += len(values)
        unchanged_rows += skipped
        processed += len(rows)

        if pk_cols:
            cursor = tuple(rows[-1][idx] for idx in pk_indexes)
        else:
            fallback_offset += len(rows)

        elapsed = time.time() - t0
        pct     = processed / effective_total * 100
        print(f"  [{table_name}]{limit_tag} {processed:,}/{effective_total:,} ({pct:.0f}%) {elapsed:.1f}s", end="\r", flush=True)

    with vercel_engine.connect() as vercel_conn:
        vercel_after = count_rows(vercel_conn, table_name)
    elapsed      = time.time() - t0
    limit_tag    = f" [capped at {row_limit:,}]" if row_limit else ""
    detail = f" sent={sent_rows:,}"
    if unchanged_rows:
        detail += f" unchanged={unchanged_rows:,}"
    print(
        f"  [{table_name}] OK  local={local_total:,}{limit_tag}  "
        f"remote={vercel_after:,}{detail}  {elapsed:.1f}s      ",
        flush=True,
    )

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
    print(f"  Local : {make_url(LOCAL_URL).render_as_string(hide_password=True)}")
    print(f"  Remote: {make_url(VERCEL_URL).render_as_string(hide_password=True)}")
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

    enforce_article_manifest_guard(vercel_engine)

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
    ans = input(f"Proceed? {warn}(y/N): ").strip().lower()
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
    disable_fk_checks = supports_replica_role(vercel_engine)
    if disable_fk_checks:
        print("[info] FK checks disabled per batch with transaction-local scope.")
    else:
        print("[info] FK checks active; continuing safely.")

    with local_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lc:
        for tbl in selected_tables:
            print(f"  [{tbl}] Starting...", flush=True)
            migrate_table(
                lc,
                vercel_engine,
                tbl,
                mode,
                row_limit=row_limits.get(tbl),
                disable_fk_checks=disable_fk_checks,
            )

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
    acquire_lock("migrate_local_to_vercel")
    run_migration("reset" if args.reset else "incremental")
