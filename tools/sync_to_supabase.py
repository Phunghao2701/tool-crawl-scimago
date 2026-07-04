"""
sync_to_supabase.py
-------------------
Tool dong bo Local PostgreSQL -> Supabase.

2 phuong phap:
  - FULL pg_dump : dung COPY format, nhanh nhat (~2-5 min)
  - LIMITED      : Python batch insert voi gioi han so dong theo bang

Chay: python tools/sync_to_supabase.py
"""
import os, sys, time, subprocess, tempfile, re, json
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
from sqlalchemy import create_engine, text, pool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env.local"), override=False)
LOCAL_URL = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")

load_dotenv(os.path.join(BASE_DIR, ".env.vercel"), override=True)
REMOTE_URL = os.getenv("VERCEL_DATABASE_URL")

# Supabase raw URL cho psql (khong co driver prefix)
SUPABASE_PSQL_URL = "postgresql://postgres.egyrzaqtmxmcezxchfrl:TeamSWP3912006@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres?sslmode=require"
DOCKER_CONTAINER   = "scientific_journal_postgres"
LOCAL_DB           = "scientific_journal_db"
LOCAL_PG_USER      = "postgres"

# ── Table order (FK: parents truoc children) ──────────────────────────────────
TABLES_ORDER = [
    "user",
    "Password_Reset_Token",
    "Zone", 
    "Subject_Area", 
    "Subject_Category", 
    "Publisher", 
    "Ranking_Metric",
    "Journal", 
    "Journal_Subject_Category",
    "Volume", 
    "Issue",
    "Topic", 
    "Article",
    "Author", 
    "Author_Article",
    "Keyword", 
    "Keyword_Article",
    "Sub_Topic",
    "Journal_Ranking",
    "Journal_Ranking_Subject_Category",
    "Project",
    "Project_Journal",
    "Project_Keyword",
    "Subject_Category_Project"
]

LARGE_TABLES = {
    "Article":         220_000,
    "Author":          580_000,
    "Author_Article": 1_280_000,
    "Keyword_Article": 2_200_000,
    "Sub_Topic":        580_000,
    "Journal_Ranking":  328_000,
}

CHUNK_SIZE   = 1000
COMMIT_EVERY = 5000

# ── Sync Profiles ─────────────────────────────────────────────────────────────
PROFILES = {
    "1": {
        "label": "Full pg_dump  (COPY format, nhanh nhat ~2-5 phut)",
        "method": "pgdump",
        "tables": None,
        "row_limits": {},
    },
    "2": {
        "label": "Journals only  (~500k rows, ~3 min)",
        "method": "python",
        "tables": [
            "Zone", "Subject_Area", "Subject_Category", "Publisher",
            "Ranking_Metric", "Journal", "Journal_Subject_Category",
            "Journal_Ranking", "Journal_Ranking_Subject_Category",
        ],
        "row_limits": {},
    },
    "3": {
        "label": "Journals + Articles  (~800k rows, ~8 min)",
        "method": "python",
        "tables": [
            "Zone", "Subject_Area", "Subject_Category", "Publisher",
            "Ranking_Metric", "Journal", "Journal_Subject_Category",
            "Volume", "Issue", "Topic", "Article",
            "Keyword", "Keyword_Article",
            "Journal_Ranking", "Journal_Ranking_Subject_Category",
        ],
        "row_limits": {},
    },
    "4": {
        "label": "Full Python  (~5M+ rows, 30-60 min, ON CONFLICT safe)",
        "method": "python",
        "tables": None,
        "row_limits": {},
    },
    "5": {
        "label": "Custom  (chon so dong tung bang lon)",
        "method": "python",
        "tables": None,
        "row_limits": {},
    },
    "6": {
        "label": "Domain Filter Sync  (AI/Tech/Custom topics)",
        "method": "domain",
        "tables": None,
        "row_limits": {},
    },
}

# ─────────────────────────────────────────────────────────────────────────────
def make_engine(url):
    return create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 20, "options": "-c statement_timeout=600000"},
    )

def count_rows(conn, tbl):
    try:
        return conn.execute(text(f'SELECT COUNT(*) FROM "{tbl}"')).scalar()
    except Exception:
        return 0

def get_columns(conn, tbl):
    rows = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:tbl
        ORDER BY ordinal_position
    """), {"tbl": tbl}).fetchall()
    return [r[0] for r in rows]

def truncate_all_remote(engine):
    print("  [truncate] Clearing Supabase data...")
    with engine.connect() as conn:
        tbls = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).fetchall()
        tbl_list = ", ".join(f'"{t[0]}"' for t in tbls)
        conn.execute(text(f"TRUNCATE TABLE {tbl_list} RESTART IDENTITY CASCADE;"))
        conn.commit()
    print(f"  [truncate] {len(tbls)} tables cleared.")

# ── pg_dump method ────────────────────────────────────────────────────────────
def run_pgdump_full(remote_engine):
    """Export toan bo bang COPY (nhanh nhat), import vao Supabase."""
    print("\n  [pg_dump] Exporting from Docker...")
    ret = subprocess.run([
        "docker", "exec", DOCKER_CONTAINER,
        "pg_dump", "-U", LOCAL_PG_USER, "-d", LOCAL_DB,
        "--data-only", "--no-acl", "--no-owner", "--no-tablespaces",
        "-f", "/tmp/sync_dump.sql"
    ])
    if ret.returncode != 0:
        print("  [ERROR] pg_dump failed.")
        return False

    print("  [pg_dump] Importing into Supabase (COPY format)...")
    print("  (This may take 2-10 minutes...)\n")
    ret = subprocess.run([
        "docker", "exec", "-i", DOCKER_CONTAINER,
        "psql", SUPABASE_PSQL_URL,
        "-c", "SET session_replication_role = replica;",
        "-f", "/tmp/sync_dump.sql",
        "-c", "SET session_replication_role = DEFAULT;",
    ])
    return ret.returncode == 0

# ── Python batch method ───────────────────────────────────────────────────────
def get_jsonb_columns(conn, tbl):
    rows = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:tbl AND udt_name='jsonb'
        ORDER BY ordinal_position
    """), {"tbl": tbl}).fetchall()
    return {r[0] for r in rows}


def build_insert_sql(table_name, cols, jsonb_cols=None):
    jsonb_cols = set(jsonb_cols or [])
    col_list = ", ".join(f'"{c}"' for c in cols)
    param_list = ", ".join(
        f'CAST(:{c} AS JSONB)' if c in jsonb_cols else f':{c}'
        for c in cols
    )
    return f'INSERT INTO "{table_name}" ({col_list}) VALUES ({param_list}) ON CONFLICT DO NOTHING'


def serialize_row(cols, row, jsonb_cols=None):
    jsonb_cols = set(jsonb_cols or [])
    payload = {}
    for c, v in zip(cols, row):
        if c in jsonb_cols and v is not None:
            payload[c] = json.dumps(v, ensure_ascii=False)
        else:
            payload[c] = v
    return payload

def sync_table(lc, rc, tbl, row_limit=None):
    t0 = time.time()
    local_total = count_rows(lc, tbl)
    if local_total == 0:
        print(f"  [{tbl}] Empty locally, skip.", flush=True)
        return

    effective = min(local_total, row_limit) if row_limit else local_total
    remote_total = count_rows(rc, tbl)
    if remote_total >= effective:
        print(f"  [{tbl}] Already up-to-date ({remote_total:,} rows), skip.", flush=True)
        return

    l_cols = get_columns(lc, tbl)
    r_cols = set(get_columns(rc, tbl))
    cols   = [c for c in l_cols if c in r_cols]
    if not cols:
        print(f"  [{tbl}] No matching columns, skip.", flush=True)
        return

    jsonb_cols = get_jsonb_columns(rc, tbl)
    insert_sql = build_insert_sql(tbl, cols, jsonb_cols)
    col_list   = ", ".join(f'"{c}"' for c in cols)
    lim_tag    = f" [limit={row_limit:,}]" if row_limit else ""

    print(f"  [{tbl}]{lim_tag} Starting ({effective:,} rows)...", flush=True)

    offset, processed = 0, 0
    while True:
        fetch = min(CHUNK_SIZE, effective - offset) if row_limit else CHUNK_SIZE
        if fetch <= 0:
            break
        rows = lc.execute(text(
            f'SELECT {col_list} FROM "{tbl}" ORDER BY 1 LIMIT :lim OFFSET :off'
        ), {"lim": fetch, "off": offset}).fetchall()
        if not rows:
            break

        try:
            rc.execute(text(insert_sql), [serialize_row(cols, r, jsonb_cols) for r in rows])
        except Exception as e:
            print(f"\n  [ERROR] Batch insert failed for {tbl} at offset={offset:,}, fetch={fetch:,}: {e}", flush=True)
            raise
        processed += len(rows)
        if processed % COMMIT_EVERY == 0 or len(rows) < fetch:
            rc.commit()

        pct = processed / effective * 100
        print(f"  [{tbl}]{lim_tag} {processed:,}/{effective:,} ({pct:.0f}%) {time.time()-t0:.1f}s",
              end="\r", flush=True)
        offset += len(rows)

    rc.commit()
    after = count_rows(rc, tbl)
    print(f"  [{tbl}] OK  local={local_total:,}{lim_tag}  remote={after:,}  {time.time()-t0:.1f}s      ",
          flush=True)

def run_python_sync(tables, row_limits, local_engine, remote_engine):
    with local_engine.connect() as lc, remote_engine.connect() as rc:
        try:
            rc.execute(text("SET session_replication_role = replica;"))
            rc.commit()
            print("  [info] FK checks disabled.")
        except Exception:
            rc.rollback()
            print("  [info] FK checks active (Prisma proxy).")

        for tbl in tables:
            sync_table(lc, rc, tbl, row_limit=row_limits.get(tbl))

        try:
            rc.execute(text("SET session_replication_role = DEFAULT;"))
            rc.commit()
        except Exception:
            pass

# ── Domain filtered sync ──────────────────────────────────────────────────────
def normalize_terms(raw_text):
    return [x.strip().lower() for x in re.split(r"[,;\n]+", raw_text) if x.strip()]

def build_domain_match_clause(params, aliases):
    conditions = []
    for idx, term in enumerate(params["terms"]):
        key = f"term_{idx}"
        params[key] = f"%{term}%"
        for alias, column in aliases:
            conditions.append(f"LOWER({alias}.\"{column}\") LIKE :{key}")
    return " OR ".join(conditions) if conditions else "1=0"

def build_domain_context(terms, article_limit=None):
    params = {"terms": terms}
    topic_match = build_domain_match_clause(params, [
        ("t", "display_name"),
        ("sa", "display_name"),
        ("sc", "display_name"),
    ])
    keyword_match = build_domain_match_clause(params, [("k", "display_name")])
    limit_sql = f" LIMIT {int(article_limit)}" if article_limit else ""

    selected_articles_sql = f'''
        SELECT a.article_id
        FROM "Article" a
        LEFT JOIN "Topic" pt ON pt.topic_id = a.primary_topic
        LEFT JOIN "Subject_Area" psa ON psa.subject_area_id = pt.subject_area_id
        LEFT JOIN "Subject_Category" psc ON psc.subject_category_id = pt.subject_category_id
        WHERE a.is_deleted = false
          AND (
            EXISTS (
                SELECT 1
                FROM "Topic" t
                LEFT JOIN "Subject_Area" sa ON sa.subject_area_id = t.subject_area_id
                LEFT JOIN "Subject_Category" sc ON sc.subject_category_id = t.subject_category_id
                WHERE t.topic_id = a.primary_topic
                  AND ({topic_match})
            )
            OR EXISTS (
                SELECT 1
                FROM "Sub_Topic" st
                JOIN "Topic" t ON t.topic_id = st.topic_id
                LEFT JOIN "Subject_Area" sa ON sa.subject_area_id = t.subject_area_id
                LEFT JOIN "Subject_Category" sc ON sc.subject_category_id = t.subject_category_id
                WHERE st.article_id = a.article_id
                  AND ({topic_match})
            )
            OR EXISTS (
                SELECT 1
                FROM "Keyword_Article" ka
                JOIN "Keyword" k ON k.keyword_id = ka.keyword_id
                WHERE ka.article_id = a.article_id
                  AND ({keyword_match})
            )
            OR ({topic_match.replace('t.', 'pt.').replace('sa.', 'psa.').replace('sc.', 'psc.')})
          )
        ORDER BY a.article_id
        {limit_sql}
    '''

    return {
        "params": params,
        "selected_articles_sql": selected_articles_sql,
        "queries": {
            "Topic": f'''
                SELECT DISTINCT t.*
                FROM "Topic" t
                LEFT JOIN "Subject_Area" sa ON sa.subject_area_id = t.subject_area_id
                LEFT JOIN "Subject_Category" sc ON sc.subject_category_id = t.subject_category_id
                WHERE ({topic_match})
                   OR t.topic_id IN (SELECT primary_topic FROM "Article" WHERE article_id IN ({selected_articles_sql}))
                   OR t.topic_id IN (SELECT topic_id FROM "Sub_Topic" WHERE article_id IN ({selected_articles_sql}))
                ORDER BY t.topic_id
            ''',
            "Article": f'''
                SELECT a.* FROM "Article" a
                WHERE a.article_id IN ({selected_articles_sql})
                ORDER BY a.article_id
            ''',
            "Sub_Topic": f'''
                SELECT st.* FROM "Sub_Topic" st
                WHERE st.article_id IN ({selected_articles_sql})
                ORDER BY st.article_id, st.topic_id
            ''',
            "Issue": f'''
                SELECT i.* FROM "Issue" i
                WHERE i.issue_id IN (
                    SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                )
                ORDER BY i.issue_id
            ''',
            "Volume": f'''
                SELECT v.* FROM "Volume" v
                WHERE v.volume_id IN (
                    SELECT DISTINCT i.volume_id FROM "Issue" i
                    WHERE i.issue_id IN (
                        SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                    )
                )
                ORDER BY v.volume_id
            ''',
            "Journal": f'''
                SELECT j.* FROM "Journal" j
                WHERE j.journal_id IN (
                    SELECT DISTINCT v.journal_id FROM "Volume" v
                    WHERE v.volume_id IN (
                        SELECT DISTINCT i.volume_id FROM "Issue" i
                        WHERE i.issue_id IN (
                            SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                        )
                    )
                )
                ORDER BY j.journal_id
            ''',
            "Journal_Subject_Category": f'''
                SELECT jsc.* FROM "Journal_Subject_Category" jsc
                WHERE jsc.journal_id IN (
                    SELECT DISTINCT v.journal_id FROM "Volume" v
                    WHERE v.volume_id IN (
                        SELECT DISTINCT i.volume_id FROM "Issue" i
                        WHERE i.issue_id IN (
                            SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                        )
                    )
                )
                ORDER BY jsc.journal_id, jsc.subject_category_id
            ''',
            "Journal_Ranking": f'''
                SELECT jr.* FROM "Journal_Ranking" jr
                WHERE jr.journal_id IN (
                    SELECT DISTINCT v.journal_id FROM "Volume" v
                    WHERE v.volume_id IN (
                        SELECT DISTINCT i.volume_id FROM "Issue" i
                        WHERE i.issue_id IN (
                            SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                        )
                    )
                )
                ORDER BY jr.journal_ranking_id
            ''',
            "Journal_Ranking_Subject_Category": f'''
                SELECT jrsc.* FROM "Journal_Ranking_Subject_Category" jrsc
                WHERE jrsc.journal_ranking_id IN (
                    SELECT jr.journal_ranking_id FROM "Journal_Ranking" jr
                    WHERE jr.journal_id IN (
                        SELECT DISTINCT v.journal_id FROM "Volume" v
                        WHERE v.volume_id IN (
                            SELECT DISTINCT i.volume_id FROM "Issue" i
                            WHERE i.issue_id IN (
                                SELECT DISTINCT a.issue_id FROM "Article" a WHERE a.article_id IN ({selected_articles_sql}) AND a.issue_id IS NOT NULL
                            )
                        )
                    )
                )
                ORDER BY jrsc.journal_ranking_id, jrsc.subject_category_id
            ''',
            "Author_Article": f'''
                SELECT aa.* FROM "Author_Article" aa
                WHERE aa.article_id IN ({selected_articles_sql})
                ORDER BY aa.author_id, aa.article_id
            ''',
            "Author": f'''
                SELECT au.* FROM "Author" au
                WHERE au.author_id IN (
                    SELECT DISTINCT aa.author_id FROM "Author_Article" aa WHERE aa.article_id IN ({selected_articles_sql})
                )
                ORDER BY au.author_id
            ''',
            "Keyword_Article": f'''
                SELECT ka.* FROM "Keyword_Article" ka
                WHERE ka.article_id IN ({selected_articles_sql})
                ORDER BY ka.keyword_id, ka.article_id
            ''',
            "Keyword": f'''
                SELECT k.* FROM "Keyword" k
                WHERE k.keyword_id IN (
                    SELECT DISTINCT ka.keyword_id FROM "Keyword_Article" ka WHERE ka.article_id IN ({selected_articles_sql})
                )
                   OR ({keyword_match})
                ORDER BY k.keyword_id
            ''',
        }
    }

def count_query_rows(conn, query, params):
    return conn.execute(text(f"SELECT COUNT(*) FROM ({query}) q"), params).scalar() or 0

def sync_table_query(lc, rc, tbl, query, params):
    t0 = time.time()
    total = count_query_rows(lc, query, params)
    if total == 0:
        print(f"  [{tbl}] No rows match domain filter, skip.", flush=True)
        return

    remote_total = count_rows(rc, tbl)
    l_cols = get_columns(lc, tbl)
    r_cols = set(get_columns(rc, tbl))
    cols = [c for c in l_cols if c in r_cols]
    if not cols:
        print(f"  [{tbl}] No matching columns, skip.", flush=True)
        return

    insert_sql = build_insert_sql(tbl, cols)
    col_list = ", ".join(f'"{c}"' for c in cols)
    ordered_query = f'SELECT {col_list} FROM ({query}) src'

    print(f"  [{tbl}] Domain sync starting ({total:,} rows)...", flush=True)
    offset = processed = 0
    while True:
        rows = lc.execute(text(f"{ordered_query} LIMIT :lim OFFSET :off"), {**params, "lim": CHUNK_SIZE, "off": offset}).fetchall()
        if not rows:
            break
        rc.execute(text(insert_sql), [dict(zip(cols, r)) for r in rows])
        processed += len(rows)
        if processed % COMMIT_EVERY == 0 or len(rows) < CHUNK_SIZE:
            rc.commit()
        pct = processed / total * 100
        print(f"  [{tbl}] {processed:,}/{total:,} ({pct:.0f}%) {time.time()-t0:.1f}s", end="\r", flush=True)
        offset += len(rows)

    rc.commit()
    after = count_rows(rc, tbl)
    print(f"  [{tbl}] OK  filtered={total:,}  remote={after:,}  {time.time()-t0:.1f}s      ", flush=True)

def configure_domain_sync():
    print()
    print("  [Domain Filter] Vi du: artificial intelligence, machine learning, computer science")
    raw = input("  Nhap danh sach chu de/keyword muon sync: ").strip()
    terms = normalize_terms(raw)
    if not terms:
        print("  [ERROR] Ban phai nhap it nhat 1 chu de.")
        return None
    limit_raw = input("  Gioi han so Article match (Enter = lay tat ca): ").strip()
    article_limit = int(limit_raw) if limit_raw.isdigit() and int(limit_raw) > 0 else None
    return {"terms": terms, "article_limit": article_limit}

def run_domain_sync(domain_cfg, local_engine, remote_engine):
    context = build_domain_context(domain_cfg["terms"], domain_cfg.get("article_limit"))
    full_copy_tables = ["Zone", "Subject_Area", "Subject_Category", "Publisher", "Ranking_Metric"]
    filtered_tables = [
        "Topic", "Journal", "Journal_Subject_Category", "Volume", "Issue",
        "Article", "Author", "Author_Article", "Keyword", "Keyword_Article",
        "Sub_Topic", "Journal_Ranking", "Journal_Ranking_Subject_Category",
    ]

    with local_engine.connect() as lc, remote_engine.connect() as rc:
        try:
            rc.execute(text("SET session_replication_role = replica;"))
            rc.commit()
        except Exception:
            rc.rollback()

        print(f"\n  [Domain] Terms: {', '.join(domain_cfg['terms'])}")
        total_articles = count_query_rows(lc, context["queries"]["Article"], context["params"])
        print(f"  [Domain] Matching articles: {total_articles:,}")

        for tbl in full_copy_tables:
            sync_table(lc, rc, tbl)
        for tbl in filtered_tables:
            sync_table_query(lc, rc, tbl, context["queries"][tbl], context["params"])

        try:
            rc.execute(text("SET session_replication_role = DEFAULT;"))
            rc.commit()
        except Exception:
            pass

# ── Menu ──────────────────────────────────────────────────────────────────────
def select_profile():
    print("\n  Chon pham vi dong bo:")
    print("  " + "-" * 55)
    for key, p in PROFILES.items():
        print(f"  {key}. {p['label']}")
    print("  " + "-" * 55)
    while True:
        choice = input("  Chon (1-6): ").strip()
        if choice in PROFILES:
            return choice
        print("  Lua chon khong hop le.")

def resolve_tables_and_limits(choice, local_engine):
    profile    = PROFILES[choice]
    tables     = list(profile["tables"]) if profile["tables"] else list(TABLES_ORDER)
    row_limits = {}

    if choice == "5":
        print()
        print("  [Custom Config] Chon che do custom:")
        print("    1. Chi cau hinh cac bang LON (Article, Author, Keyword_Article, Sub_Topic, Journal_Ranking...)")
        print("    2. Cau hinh cho TAT CA 25 bang")
        custom_choice = input("  Chon (1/2, default 1): ").strip()

        target_tables = []
        with local_engine.connect() as lc:
            for tbl in TABLES_ORDER:
                is_large = tbl in LARGE_TABLES
                
                # Neu chon 1, lay tat ca bang nho tu dong, chi custom bang lon
                if custom_choice != "2" and not is_large:
                    target_tables.append(tbl)
                    continue

                local_cnt = count_rows(lc, tbl)
                if local_cnt == 0:
                    continue  # Bang rong tu dong bo qua

                print(f"\n  * Cau hinh bang [{tbl}] (Local co {local_cnt:,} rows):")
                sync_ans = input("    Dong bo bang nay? (Y/n): ").strip().lower()
                if sync_ans == "n":
                    print(f"    -> Bo qua [{tbl}]")
                    continue

                target_tables.append(tbl)
                val = input(f"    Nhap gioi han so dong muon sync (Enter = Lay het {local_cnt:,} rows): ").strip()
                if val.isdigit() and int(val) > 0:
                    row_limits[tbl] = int(val)
                    print(f"    -> Sync gioi han: {int(val):,} rows")
                else:
                    print("    -> Sync toan bo data")
            
            tables = target_tables

    return tables, row_limits

def print_preview(tables, row_limits, local_engine, remote_engine):
    print()
    print(f"  {'Table':<38} {'Local':>9}  {'Remote':>9}  {'Limit':>9}  Status")
    print("  " + "-" * 80)
    with local_engine.connect() as lc, remote_engine.connect() as rc:
        for tbl in tables:
            lc_ = count_rows(lc, tbl)
            rc_ = count_rows(rc, tbl)
            lim = row_limits.get(tbl)
            eff = min(lc_, lim) if lim else lc_
            status = "up-to-date" if rc_ >= eff and eff > 0 else \
                     "needs copy"  if rc_ == 0 else f"missing {eff - rc_:,}"
            lim_str = f"{lim:,}" if lim else "-"
            print(f"  {tbl:<38} {lc_:>9,}  {rc_:>9,}  {lim_str:>9}  {status}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("=" * 60)
    print("  LOCAL -> SUPABASE SYNC")
    print("=" * 60)
    print(f"  Local : {LOCAL_URL[:55]}...")
    print(f"  Remote: {REMOTE_URL[:55]}...")

    local_engine  = make_engine(LOCAL_URL)
    remote_engine = make_engine(REMOTE_URL)

    # Kiem tra ket noi
    with local_engine.connect() as lc:
        db = lc.execute(text("SELECT current_database()")).scalar()
        print(f"\n  [check] Local  DB... {db} [OK]")
    with remote_engine.connect() as rc:
        db = rc.execute(text("SELECT current_database()")).scalar()
        print(f"  [check] Remote DB... {db} [OK]")

    choice = select_profile()
    profile = PROFILES[choice]

    if choice == "1":
        # pg_dump: truncate truoc roi import
        print()
        warn = "Toan bo data Supabase se bi XOA truoc khi import!"
        ans  = input(f"  [!] {warn}\n  Tiep tuc? (yes/N): ").strip().lower()
        if ans != "yes":
            print("Cancelled.")
            return

        t0 = time.time()
        truncate_all_remote(remote_engine)
        ok = run_pgdump_full(remote_engine)
        if ok:
            print(f"\n  [OK] pg_dump sync done in {time.time()-t0:.0f}s!")
        else:
            print("\n  [ERROR] Import failed. Check docker and psql availability.")
        return

    if choice == "6":
        domain_cfg = configure_domain_sync()
        if not domain_cfg:
            return
        context = build_domain_context(domain_cfg["terms"], domain_cfg.get("article_limit"))
        with local_engine.connect() as lc:
            match_count = count_query_rows(lc, context["queries"]["Article"], context["params"])
        print(f"\n  [preview] Article match domain filter: {match_count:,}")
        print("  [note] Option nay KHONG xoa data cu tren Supabase; chi chen them rows match filter.")
        ans = input("  Proceed domain sync? (y/N): ").strip().lower()
        if ans != "y":
            print("Cancelled.")
            return
        t0 = time.time()
        run_domain_sync(domain_cfg, local_engine, remote_engine)
        print(f"\n  [OK] Domain sync done in {time.time()-t0:.1f}s!")
        return

    # Python method
    tables, row_limits = resolve_tables_and_limits(choice, local_engine)
    print_preview(tables, row_limits, local_engine, remote_engine)

    print()
    ans = input("  Proceed? (y/N): ").strip().lower()
    if ans != "y":
        print("Cancelled.")
        return

    t0 = time.time()
    print(f"\n  Syncing {len(tables)} tables (Python batch method)...")
    run_python_sync(tables, row_limits, local_engine, remote_engine)

    elapsed = time.time() - t0
    print(f"\n  [OK] Done in {elapsed:.1f}s!")

    # Final counts
    print("\n  [verify] Final row counts on Supabase:")
    with remote_engine.connect() as rc:
        for tbl in tables:
            cnt = count_rows(rc, tbl)
            if cnt > 0:
                print(f"    {tbl}: {cnt:,}")


if __name__ == "__main__":
    main()
