"""
Restore a Supabase subset from local PostgreSQL, prioritizing articles that have merged references.

Default behavior:
- Select 10,000 Article rows.
- Prioritize rows where reference_count > 0 or references is a non-empty JSON array.
- Copy the dependent rows needed by those articles.
- Upsert into Supabase without truncating existing data.

Usage:
  python tools/sync_priority_articles_to_supabase.py
  python tools/sync_priority_articles_to_supabase.py --limit 10000
"""

import argparse
import json
import os
import sys
import time
from typing import Iterable, Sequence

from dotenv import load_dotenv
from sqlalchemy import create_engine, text, pool

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env.local"), override=False)
LOCAL_URL = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")

load_dotenv(os.path.join(BASE_DIR, ".env.vercel"), override=True)
REMOTE_URL = os.getenv("VERCEL_DATABASE_URL")

CHUNK_SIZE = 1000


def make_engine(url: str):
    return create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 20, "options": "-c statement_timeout=600000"},
    )


def get_columns(conn, table_name: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table_name
            ORDER BY ordinal_position
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    return [r[0] for r in rows]


def get_pk_columns(conn, table_name: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = :table_name
            ORDER BY kcu.ordinal_position
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    return [r[0] for r in rows]


def get_jsonb_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table_name AND udt_name='jsonb'
            ORDER BY ordinal_position
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    return {r[0] for r in rows}


def count_rows(conn, table_name: str) -> int:
    return conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0


def build_upsert_sql(table_name: str, cols: Sequence[str], pk_cols: Sequence[str], jsonb_cols: set[str] | None = None) -> str:
    jsonb_cols = set(jsonb_cols or [])
    col_list = ", ".join(f'"{c}"' for c in cols)
    val_list = ", ".join(f'CAST(:{c} AS JSONB)' if c in jsonb_cols else f":{c}" for c in cols)
    if pk_cols:
        conflict_cols = ", ".join(f'"{c}"' for c in pk_cols)
        update_cols = [c for c in cols if c not in pk_cols]
        if update_cols:
            update_set = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
            conflict = f"ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}"
        else:
            conflict = f"ON CONFLICT ({conflict_cols}) DO NOTHING"
    else:
        conflict = "ON CONFLICT DO NOTHING"
    return f'INSERT INTO "{table_name}" ({col_list}) VALUES ({val_list}) {conflict}'


def serialize_row(cols: Sequence[str], row, jsonb_cols: set[str] | None = None) -> dict:
    jsonb_cols = set(jsonb_cols or [])
    payload = {}
    for c, v in zip(cols, row):
        payload[c] = json.dumps(v, ensure_ascii=False) if c in jsonb_cols and v is not None else v
    return payload


def fetch_ids(conn, sql: str, params: dict | None = None) -> list[int]:
    rows = conn.execute(text(sql), params or {}).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def batched(values: Sequence[int], size: int = CHUNK_SIZE) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield list(values[i : i + size])


def sync_query(lc, rc, table_name: str, query_sql: str, params: dict | None = None) -> int:
    l_cols = get_columns(lc, table_name)
    r_cols = set(get_columns(rc, table_name))
    cols = [c for c in l_cols if c in r_cols]
    if not cols:
        print(f"  [{table_name}] No matching columns, skipped.")
        return 0

    pk_cols = [c for c in get_pk_columns(rc, table_name) if c in cols]
    jsonb_cols = get_jsonb_columns(rc, table_name)
    upsert_sql = build_upsert_sql(table_name, cols, pk_cols, jsonb_cols)
    col_list = ", ".join(f'"{c}"' for c in cols)
    ordered_query = f"SELECT {col_list} FROM ({query_sql}) src"

    total = lc.execute(text(f"SELECT COUNT(*) FROM ({query_sql}) q"), params or {}).scalar() or 0
    if total == 0:
        print(f"  [{table_name}] 0 row, skipped.")
        return 0

    print(f"  [{table_name}] syncing {total:,} row(s)...")
    processed = 0
    offset = 0
    while True:
        rows = lc.execute(
            text(f"{ordered_query} LIMIT :limit OFFSET :offset"),
            {**(params or {}), "limit": CHUNK_SIZE, "offset": offset},
        ).fetchall()
        if not rows:
            break
        rc.execute(text(upsert_sql), [serialize_row(cols, row, jsonb_cols) for row in rows])
        processed += len(rows)
        offset += len(rows)
        if processed % 5000 == 0 or len(rows) < CHUNK_SIZE:
            rc.commit()
        print(f"    {processed:,}/{total:,}", end="\r", flush=True)
    rc.commit()
    print(f"  [{table_name}] done {processed:,}/{total:,}        ")
    return processed


def sync_by_ids(lc, rc, table_name: str, id_column: str, ids: Sequence[int]) -> int:
    if not ids:
        print(f"  [{table_name}] No ids, skipped.")
        return 0
    total = 0
    for chunk in batched(ids):
        query = f'SELECT * FROM "{table_name}" WHERE "{id_column}" = ANY(:ids) ORDER BY "{id_column}"'
        total += sync_query(lc, rc, table_name, query, {"ids": chunk})
    return total


def main():
    parser = argparse.ArgumentParser(description="Restore priority 10k article subset to Supabase")
    parser.add_argument("--limit", type=int, default=10_000, help="Article limit, default 10000")
    parser.add_argument("--yes", action="store_true", help="Run without confirmation prompt")
    args = parser.parse_args()

    if not LOCAL_URL or not REMOTE_URL:
        raise SystemExit("Missing LOCAL_DATABASE_URL/DATABASE_URL or VERCEL_DATABASE_URL")

    local_engine = make_engine(LOCAL_URL)
    remote_engine = make_engine(REMOTE_URL)

    t0 = time.time()
    with local_engine.connect() as lc, remote_engine.connect() as rc:
        print("=" * 70)
        print("LOCAL -> SUPABASE PRIORITY ARTICLE RESTORE")
        print("=" * 70)
        print(f"Limit: {args.limit:,} articles")
        print("Priority: references/reference_count first")
        print()

        local_db = lc.execute(text("SELECT current_database()")).scalar()
        remote_db = rc.execute(text("SELECT current_database()")).scalar()
        print(f"Local DB : {local_db}")
        print(f"Remote DB: {remote_db}")

        selected_article_sql = """
            SELECT a.article_id
            FROM "Article" a
            WHERE COALESCE(a.is_deleted, false) = false
            ORDER BY
              CASE
                WHEN COALESCE(a.reference_count, 0) > 0 THEN 0
                WHEN a."references" IS NOT NULL
                 AND jsonb_typeof(a."references") = 'array'
                 AND jsonb_array_length(a."references") > 0 THEN 0
                ELSE 1
              END,
              COALESCE(a.reference_count, 0) DESC,
              a.article_id ASC
            LIMIT :limit
        """
        article_ids = fetch_ids(lc, selected_article_sql, {"limit": args.limit})
        with_refs = lc.execute(
            text(
                """
                SELECT COUNT(*)
                FROM "Article"
                WHERE article_id = ANY(:ids)
                  AND (
                    COALESCE(reference_count, 0) > 0
                    OR (
                      "references" IS NOT NULL
                      AND jsonb_typeof("references") = 'array'
                      AND jsonb_array_length("references") > 0
                    )
                  )
                """
            ),
            {"ids": article_ids},
        ).scalar() or 0

        print(f"Selected articles: {len(article_ids):,}")
        print(f"Selected with references: {with_refs:,}")
        print()

        if not args.yes:
            ans = input("Proceed upsert to Supabase? (y/N): ").strip().lower()
            if ans != "y":
                print("Cancelled.")
                return

        try:
            rc.execute(text("SET session_replication_role = replica;"))
            rc.commit()
            print("[info] FK checks disabled.")
        except Exception:
            rc.rollback()
            print("[info] FK checks active.")

        # Full/small lookup tables needed across UI.
        for table_name in ["Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric"]:
            sync_query(lc, rc, table_name, f'SELECT * FROM "{table_name}" ORDER BY 1')

        issue_ids = fetch_ids(
            lc,
            'SELECT DISTINCT issue_id FROM "Article" WHERE article_id = ANY(:ids) AND issue_id IS NOT NULL ORDER BY issue_id',
            {"ids": article_ids},
        )
        volume_ids = fetch_ids(
            lc,
            'SELECT DISTINCT volume_id FROM "Issue" WHERE issue_id = ANY(:ids) AND volume_id IS NOT NULL ORDER BY volume_id',
            {"ids": issue_ids},
        )
        journal_ids = fetch_ids(
            lc,
            'SELECT DISTINCT journal_id FROM "Volume" WHERE volume_id = ANY(:ids) AND journal_id IS NOT NULL ORDER BY journal_id',
            {"ids": volume_ids},
        )
        topic_ids = fetch_ids(
            lc,
            'SELECT DISTINCT primary_topic FROM "Article" WHERE article_id = ANY(:ids) AND primary_topic IS NOT NULL ORDER BY primary_topic',
            {"ids": article_ids},
        )
        topic_ids += fetch_ids(
            lc,
            'SELECT DISTINCT topic_id FROM "Sub_Topic" WHERE article_id = ANY(:ids) AND topic_id IS NOT NULL ORDER BY topic_id',
            {"ids": article_ids},
        )
        topic_ids = sorted(set(topic_ids))

        print()
        print(f"Dependent journals: {len(journal_ids):,}")
        print(f"Dependent volumes : {len(volume_ids):,}")
        print(f"Dependent issues  : {len(issue_ids):,}")
        print(f"Dependent topics  : {len(topic_ids):,}")
        print()

        sync_by_ids(lc, rc, "Topic", "topic_id", topic_ids)
        sync_by_ids(lc, rc, "Journal", "journal_id", journal_ids)
        sync_query(
            lc,
            rc,
            "Journal_Subject_Category",
            'SELECT * FROM "Journal_Subject_Category" WHERE journal_id = ANY(:ids) ORDER BY journal_id, subject_category_id',
            {"ids": journal_ids},
        )
        sync_by_ids(lc, rc, "Volume", "volume_id", volume_ids)
        sync_by_ids(lc, rc, "Issue", "issue_id", issue_ids)
        sync_query(
            lc,
            rc,
            "Journal_Ranking",
            'SELECT * FROM "Journal_Ranking" WHERE journal_id = ANY(:ids) ORDER BY journal_ranking_id',
            {"ids": journal_ids},
        )
        sync_query(
            lc,
            rc,
            "Journal_Ranking_Subject_Category",
            '''
            SELECT jrsc.*
            FROM "Journal_Ranking_Subject_Category" jrsc
            JOIN "Journal_Ranking" jr ON jr.journal_ranking_id = jrsc.journal_ranking_id
            WHERE jr.journal_id = ANY(:ids)
            ORDER BY jrsc.journal_ranking_id, jrsc.subject_category_id
            ''',
            {"ids": journal_ids},
        )

        sync_query(
            lc,
            rc,
            "Article",
            f'''
            SELECT a.*
            FROM "Article" a
            WHERE a.article_id = ANY(:ids)
            ORDER BY
              CASE
                WHEN COALESCE(a.reference_count, 0) > 0 THEN 0
                WHEN a."references" IS NOT NULL
                 AND jsonb_typeof(a."references") = 'array'
                 AND jsonb_array_length(a."references") > 0 THEN 0
                ELSE 1
              END,
              COALESCE(a.reference_count, 0) DESC,
              a.article_id ASC
            ''',
            {"ids": article_ids},
        )
        sync_query(
            lc,
            rc,
            "Sub_Topic",
            'SELECT * FROM "Sub_Topic" WHERE article_id = ANY(:ids) ORDER BY article_id, topic_id',
            {"ids": article_ids},
        )
        sync_query(
            lc,
            rc,
            "Author",
            '''
            SELECT au.*
            FROM "Author" au
            WHERE au.author_id IN (
                SELECT DISTINCT author_id FROM "Author_Article" WHERE article_id = ANY(:ids)
            )
            ORDER BY au.author_id
            ''',
            {"ids": article_ids},
        )
        sync_query(
            lc,
            rc,
            "Author_Article",
            'SELECT * FROM "Author_Article" WHERE article_id = ANY(:ids) ORDER BY author_id, article_id',
            {"ids": article_ids},
        )
        sync_query(
            lc,
            rc,
            "Keyword",
            '''
            SELECT k.*
            FROM "Keyword" k
            WHERE k.keyword_id IN (
                SELECT DISTINCT keyword_id FROM "Keyword_Article" WHERE article_id = ANY(:ids)
            )
            ORDER BY k.keyword_id
            ''',
            {"ids": article_ids},
        )
        sync_query(
            lc,
            rc,
            "Keyword_Article",
            'SELECT * FROM "Keyword_Article" WHERE article_id = ANY(:ids) ORDER BY keyword_id, article_id',
            {"ids": article_ids},
        )

        try:
            rc.execute(text("SET session_replication_role = DEFAULT;"))
            rc.commit()
        except Exception:
            pass

        print()
        print("[verify] Supabase counts after sync:")
        for table_name in [
            "Journal", "Volume", "Issue", "Article", "Author", "Author_Article",
            "Keyword", "Keyword_Article", "Topic", "Sub_Topic", "Journal_Ranking",
            "Journal_Ranking_Subject_Category",
        ]:
            print(f"  {table_name}: {count_rows(rc, table_name):,}")

    print(f"\n[OK] Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
