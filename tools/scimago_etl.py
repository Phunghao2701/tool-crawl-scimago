"""
Scimago ETL Tool
Usage:
  python tools/scimago_etl.py import --file data/scimago_2025.xls --year 2025 --limit 100
  python tools/scimago_etl.py import --url "https://www.scimagojr.com/journalrank.php?out=xls" --year 2024
  python tools/scimago_etl.py stats
"""

import argparse
import io
import json
import os
import re
import sys
import uuid
from decimal import Decimal, InvalidOperation

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Lấy đường dẫn tuyệt đối tới file .env nằm ở thư mục gốc của project
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path, override=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5433/scientific_journal_db",
)

SCIMAGO_DEFAULT_URL = "https://www.scimagojr.com/journalrank.php?out=xls"

# Map cột Scimago → tên cột staging
COLUMN_MAP = {
    "Rank": "rank_txt",
    "Sourceid": "source_id",
    "Title": "title",
    "Type": "type",
    "Issn": "issn",
    "Publisher": "publisher",
    "Open Access": "open_access",
    "Open Access Diamond": "open_access_diamond",
    "SJR": "sjr",
    "H index": "h_index",
    "Total Docs. (2024)": "total_docs_current_year",
    "Total Docs. (2025)": "total_docs_current_year",
    "Total Docs. (3years)": "total_docs_3years",
    "Total Refs.": "total_refs",
    "Total Cites (3years)": "total_cites_3years",
    "Citable Docs. (3years)": "citable_docs_3years",
    "Cites / Doc. (2years)": "cites_doc_2years",
    "Ref. / Doc.": "ref_doc",
    "Country": "country",
    "Region": "region",
    "Categories": "categories",
    "Areas": "areas",
}


# ---------------------------------------------------------------
# Download / Read
# ---------------------------------------------------------------

def download_scimago(url: str) -> bytes:
    print(f"[download] {url}")
    # Scimago yêu cầu browser-like headers, nếu vẫn bị 403 thì download thủ công
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.scimagojr.com/journalrank.php",
        "DNT": "1",
        "Connection": "keep-alive",
    }
    session = requests.Session()
    # Visit homepage first to get cookies
    try:
        session.get("https://www.scimagojr.com/", headers=headers, timeout=30)
    except Exception:
        pass
    resp = session.get(url, headers=headers, timeout=120)
    if resp.status_code == 403:
        print()
        print("[ERROR] Scimago returned 403 -- blocked by anti-bot protection.")
        print("[INFO]  Please download the file manually from your browser:")
        print(f"        {url}")
        print("[INFO]  Then run with --file:")
        print("        python tools/scimago_etl.py import --file data/scimago_2024.xls --year 2024 --limit 100")
        print()
        raise SystemExit(1)
    resp.raise_for_status()
    return resp.content


def read_scimago_bytes(content: bytes) -> pd.DataFrame:
    """
    Scimago export dùng đuôi .xls nhưng thực chất là text/csv ngăn bằng dấu ;
    """
    for encoding in ["utf-8-sig", "utf-8", "latin1"]:
        try:
            df = pd.read_csv(
                io.BytesIO(content),
                sep=";",
                dtype=str,
                encoding=encoding,
                keep_default_na=False,
                on_bad_lines="skip",
                engine="python",
            )
            print(f"[read] encoding={encoding}, rows={len(df)}, cols={list(df.columns)}")
            return df
        except Exception as exc:
            print(f"[read] encoding={encoding} failed: {exc}")
    raise RuntimeError("Cannot parse Scimago file with any known encoding.")


# ---------------------------------------------------------------
# Normalize helpers
# ---------------------------------------------------------------

def norm_bool(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None


def norm_int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return int(float(s))
    except (ValueError, OverflowError):
        return None


def norm_decimal(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if not s or s == "-":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def split_issns(v: str):
    if not v:
        return []
    parts = re.split(r"[,;/\s]+", v)
    cleaned = []
    for p in parts:
        p = p.strip().replace("-", "").upper()
        if re.match(r"^\d{7}[\dX]$", p):
            cleaned.append(p)
    return cleaned


def parse_categories(v: str):
    """
    'Oncology (Q1); Hematology (Q2)' →
    [{'name': 'Oncology', 'quartile': 'Q1'}, ...]
    """
    if not v:
        return []
    result = []
    for chunk in [x.strip() for x in v.split(";") if x.strip()]:
        m = re.match(r"^(.*?)\s*\((Q[1-4])\)\s*$", chunk)
        if m:
            result.append({"name": m.group(1).strip(), "quartile": m.group(2)})
        else:
            result.append({"name": chunk, "quartile": None})
    return result


def best_quartile(quartiles):
    order = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    valid = [q for q in quartiles if q in order]
    if not valid:
        return None
    return min(valid, key=lambda q: order[q])


# ---------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------

def upsert_zone(conn, name: str, zone_type: str):
    if not name or not name.strip():
        return None
    row = conn.execute(text("""
        INSERT INTO zone (name, type)
        VALUES (:name, :type)
        ON CONFLICT (name, type) DO UPDATE SET name = EXCLUDED.name
        RETURNING zone_id
    """), {"name": name.strip(), "type": zone_type}).fetchone()
    return row[0]


def upsert_publisher(conn, name: str):
    if not name or not name.strip():
        return None
    row = conn.execute(text("""
        INSERT INTO publisher (display_name)
        VALUES (:name)
        ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING publisher_id
    """), {"name": name.strip()}).fetchone()
    return row[0]


def upsert_metric(conn, code, display_name, metric_type):
    row = conn.execute(text("""
        INSERT INTO ranking_metric (code, display_name, metric_type)
        VALUES (:code, :display_name, :metric_type)
        ON CONFLICT (code) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            metric_type  = EXCLUDED.metric_type
        RETURNING metric_id
    """), {"code": code, "display_name": display_name, "metric_type": metric_type}).fetchone()
    return row[0]


def upsert_subject_area(conn, name: str):
    row = conn.execute(text("""
        INSERT INTO subject_area (display_name)
        VALUES (:name)
        ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING subject_area_id
    """), {"name": name.strip()}).fetchone()
    return row[0]


def upsert_subject_category(conn, area_id: int, name: str):
    row = conn.execute(text("""
        INSERT INTO subject_category (subject_area_id, display_name)
        VALUES (:area_id, :name)
        ON CONFLICT (subject_area_id, display_name)
        DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING subject_category_id
    """), {"area_id": area_id, "name": name.strip()}).fetchone()
    return row[0]


def insert_ranking(conn, journal_id, category_id, metric_id, source, year,
                   value_txt=None, value_int=None, value_float=None):
    if value_txt is None and value_int is None and value_float is None:
        return
    conn.execute(text("""
        INSERT INTO journal_ranking
            (journal_id, subject_category_id, source, metric_id, year,
             value_txt, value_int, value_float)
        VALUES
            (:jid, :catid, :src, :mid, :yr,
             :vtxt, :vint, :vfloat)
        ON CONFLICT DO NOTHING
    """), {
        "jid": journal_id, "catid": category_id,
        "src": source, "mid": metric_id, "yr": year,
        "vtxt": value_txt, "vint": value_int,
        "vfloat": float(value_float) if value_float is not None else None,
    })


# ---------------------------------------------------------------
# Stage insert
# ---------------------------------------------------------------

def build_col_map(df_columns):
    """Khớp cột thực tế trong file với COLUMN_MAP (flexible year column)."""
    mapping = {}
    for col in df_columns:
        col_stripped = col.strip()
        if col_stripped in COLUMN_MAP:
            mapping[col_stripped] = COLUMN_MAP[col_stripped]
        # handle dynamic year col: "Total Docs. (2023)", etc.
        elif re.match(r"Total Docs\. \(\d{4}\)", col_stripped):
            mapping[col_stripped] = "total_docs_current_year"
    return mapping


def insert_raw(engine, df: pd.DataFrame, batch_id: str):
    col_map = build_col_map(df.columns)
    rows = []
    for _, row in df.iterrows():
        item = {"import_batch_id": batch_id}
        for original_col, target_col in col_map.items():
            item[target_col] = row.get(original_col, "")
        # fill missing staging cols with empty string
        staging_cols = set(COLUMN_MAP.values())
        for c in staging_cols:
            if c not in item:
                item[c] = ""
        item["raw_json"] = json.dumps(row.to_dict(), ensure_ascii=False)
        rows.append(item)

    with engine.begin() as conn:
        for item in rows:
            conn.execute(text("""
                INSERT INTO raw_scimago_journal (
                    import_batch_id, rank_txt, source_id, title, type, issn,
                    publisher, open_access, open_access_diamond, sjr, h_index,
                    total_docs_current_year, total_docs_3years, total_refs,
                    total_cites_3years, citable_docs_3years, cites_doc_2years,
                    ref_doc, country, region, categories, areas, raw_json
                ) VALUES (
                    :import_batch_id, :rank_txt, :source_id, :title, :type, :issn,
                    :publisher, :open_access, :open_access_diamond, :sjr, :h_index,
                    :total_docs_current_year, :total_docs_3years, :total_refs,
                    :total_cites_3years, :citable_docs_3years, :cites_doc_2years,
                    :ref_doc, :country, :region, :categories, :areas,
                    CAST(:raw_json AS JSONB)
                )
            """), item)
    print(f"[stage] inserted {len(rows)} rows -> raw_scimago_journal")


# ---------------------------------------------------------------
# Normalize to main tables
# ---------------------------------------------------------------

def normalize(engine, batch_id: str, year: int):
    with engine.begin() as conn:
        # pre-load metric ids
        metrics = {
            "RANK":                    upsert_metric(conn, "RANK", "Rank", "INTEGER"),
            "SJR":                     upsert_metric(conn, "SJR", "SJR Score", "SCORE"),
            "SJR_BEST_QUARTILE":       upsert_metric(conn, "SJR_BEST_QUARTILE", "SJR Best Quartile", "QUARTILE"),
            "SJR_QUARTILE_BY_CAT":     upsert_metric(conn, "SJR_QUARTILE_BY_CAT", "SJR Quartile By Category", "QUARTILE"),
            "H_INDEX":                 upsert_metric(conn, "H_INDEX", "H Index", "INTEGER"),
            "TOTAL_DOCS_CURRENT_YEAR": upsert_metric(conn, "TOTAL_DOCS_CURRENT_YEAR", "Total Docs (Current Year)", "INTEGER"),
            "TOTAL_DOCS_3YEARS":       upsert_metric(conn, "TOTAL_DOCS_3YEARS", "Total Docs (3 Years)", "INTEGER"),
            "TOTAL_REFS":              upsert_metric(conn, "TOTAL_REFS", "Total Refs", "INTEGER"),
            "TOTAL_CITES_3YEARS":      upsert_metric(conn, "TOTAL_CITES_3YEARS", "Total Cites (3 Years)", "INTEGER"),
            "CITABLE_DOCS_3YEARS":     upsert_metric(conn, "CITABLE_DOCS_3YEARS", "Citable Docs (3 Years)", "INTEGER"),
            "CITES_PER_DOC_2YEARS":    upsert_metric(conn, "CITES_PER_DOC_2YEARS", "Cites / Doc (2 Years)", "SCORE"),
            "REF_PER_DOC":             upsert_metric(conn, "REF_PER_DOC", "Ref / Doc", "SCORE"),
        }

        raw_rows = conn.execute(text(
            "SELECT * FROM raw_scimago_journal WHERE import_batch_id = :bid"
        ), {"bid": batch_id}).mappings().all()

        ok = skipped = 0
        for raw in raw_rows:
            src_id = (raw["source_id"] or "").strip()
            title  = (raw["title"] or "").strip()
            if not src_id or not title:
                skipped += 1
                continue

            # Zone upserts
            country_id = upsert_zone(conn, raw["country"], "country")
            region_id  = upsert_zone(conn, raw["region"],  "region")

            # Publisher upsert
            pub_id = upsert_publisher(conn, raw["publisher"])

            # Journal upsert
            j_row = conn.execute(text("""
                INSERT INTO journal
                    (source_id, publisher_id, country_id, region_id,
                     display_name, type, is_open_access, is_oa_diamond, issn)
                VALUES
                    (:src_id, :pub_id, :country_id, :region_id,
                     :display_name, :type, :is_oa, :is_dia, :issn)
                ON CONFLICT (source_id) DO UPDATE SET
                    publisher_id  = EXCLUDED.publisher_id,
                    country_id    = EXCLUDED.country_id,
                    region_id     = EXCLUDED.region_id,
                    display_name  = EXCLUDED.display_name,
                    type          = EXCLUDED.type,
                    is_open_access = EXCLUDED.is_open_access,
                    is_oa_diamond  = EXCLUDED.is_oa_diamond,
                    issn          = EXCLUDED.issn
                RETURNING journal_id
            """), {
                "src_id":      src_id,
                "pub_id":      pub_id,
                "country_id":  country_id,
                "region_id":   region_id,
                "display_name": title,
                "type":        (raw["type"] or "").strip() or None,
                "is_oa":       norm_bool(raw["open_access"]),
                "is_dia":      norm_bool(raw["open_access_diamond"]),
                "issn":        (raw["issn"] or "").strip() or None,
            }).fetchone()
            jid = j_row[0]

            # Subject categories
            categories = parse_categories(raw["categories"])
            quartiles_all = []
            for cat in categories:
                # Area: use "areas" field if available, else derive from category name
                area_name = (raw.get("areas") or "").strip() or "General"
                area_id   = upsert_subject_area(conn, area_name)
                cat_id    = upsert_subject_category(conn, area_id, cat["name"])

                # journal ↔ category link
                conn.execute(text("""
                    INSERT INTO journal_subject_category (journal_id, subject_category_id)
                    VALUES (:jid, :cid)
                    ON CONFLICT DO NOTHING
                """), {"jid": jid, "cid": cat_id})

                # quartile per category
                if cat["quartile"]:
                    insert_ranking(conn, jid, cat_id,
                                   metrics["SJR_QUARTILE_BY_CAT"],
                                   "SCIMAGO", year,
                                   value_txt=cat["quartile"])
                    quartiles_all.append(cat["quartile"])

            # Best quartile (no category)
            bq = best_quartile(quartiles_all)
            insert_ranking(conn, jid, None, metrics["SJR_BEST_QUARTILE"],
                           "SCIMAGO", year, value_txt=bq)

            # Numeric metrics
            insert_ranking(conn, jid, None, metrics["RANK"],
                           "SCIMAGO", year, value_int=norm_int(raw["rank_txt"]))
            insert_ranking(conn, jid, None, metrics["SJR"],
                           "SCIMAGO", year, value_float=norm_decimal(raw["sjr"]))
            insert_ranking(conn, jid, None, metrics["H_INDEX"],
                           "SCIMAGO", year, value_int=norm_int(raw["h_index"]))
            insert_ranking(conn, jid, None, metrics["TOTAL_DOCS_CURRENT_YEAR"],
                           "SCIMAGO", year, value_int=norm_int(raw["total_docs_current_year"]))
            insert_ranking(conn, jid, None, metrics["TOTAL_DOCS_3YEARS"],
                           "SCIMAGO", year, value_int=norm_int(raw["total_docs_3years"]))
            insert_ranking(conn, jid, None, metrics["TOTAL_REFS"],
                           "SCIMAGO", year, value_int=norm_int(raw["total_refs"]))
            insert_ranking(conn, jid, None, metrics["TOTAL_CITES_3YEARS"],
                           "SCIMAGO", year, value_int=norm_int(raw["total_cites_3years"]))
            insert_ranking(conn, jid, None, metrics["CITABLE_DOCS_3YEARS"],
                           "SCIMAGO", year, value_int=norm_int(raw["citable_docs_3years"]))
            insert_ranking(conn, jid, None, metrics["CITES_PER_DOC_2YEARS"],
                           "SCIMAGO", year, value_float=norm_decimal(raw["cites_doc_2years"]))
            insert_ranking(conn, jid, None, metrics["REF_PER_DOC"],
                           "SCIMAGO", year, value_float=norm_decimal(raw["ref_doc"]))

            ok += 1

        print(f"[normalize] ok={ok}, skipped={skipped}")


# ---------------------------------------------------------------
# Commands
# ---------------------------------------------------------------

def cmd_import(args):
    engine = create_engine(DATABASE_URL)

    if args.file:
        with open(args.file, "rb") as f:
            content = f.read()
        print(f"[import] file={args.file}")
    else:
        content = download_scimago(args.url)

    df = read_scimago_bytes(content)

    if args.limit:
        df = df.head(args.limit)
        print(f"[import] limit={args.limit}")

    batch_id = str(uuid.uuid4())
    print(f"[import] batch_id={batch_id}, rows={len(df)}, year={args.year}")

    insert_raw(engine, df, batch_id)
    normalize(engine, batch_id, args.year)

    print(f"\n[OK] Import done! batch_id={batch_id}")


def cmd_stats(args):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        tables = [
            "raw_scimago_journal",
            "publisher", "zone", "journal", "journal_issn",
            "subject_area", "subject_category", "journal_subject_category",
            "ranking_metric", "journal_ranking",
        ]
        print("\n[DB] Database stats:")
        print(f"{'Table':<35} {'Rows':>8}")
        print("-" * 45)
        for t in tables:
            try:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"  {t:<33} {n:>8,}")
            except Exception as exc:
                print(f"  {t:<33} ERROR: {exc}")

        print("\n[DB] Sample journals:")
        rows = conn.execute(text("""
            SELECT j.source_id, j.display_name, j.type,
                   j.is_open_access, z.name AS country
            FROM journal j
            LEFT JOIN zone z ON z.zone_id = j.country_id
            ORDER BY j.journal_id
            LIMIT 10
        """)).fetchall()
        for r in rows:
            oa = "OA" if r.is_open_access else "  "
            print(f"  [{oa}] {r.source_id:>10}  {r.display_name[:55]:<55}  {r.country or '':>20}")

        print("\n[DB] Sample rankings (SJR):")
        rows = conn.execute(text("""
            SELECT j.display_name, jr.year, jr.value_float
            FROM journal_ranking jr
            JOIN journal j        ON j.journal_id = jr.journal_id
            JOIN ranking_metric m ON m.metric_id  = jr.metric_id
            WHERE m.code = 'SJR' AND jr.value_float IS NOT NULL
            ORDER BY jr.value_float DESC
            LIMIT 10
        """)).fetchall()
        for r in rows:
            print(f"  SJR={r.value_float:.4f}  {r.year}  {r.display_name[:60]}")


def main():
    parser = argparse.ArgumentParser(description="Scimago ETL Tool")
    sub = parser.add_subparsers(dest="command")

    # import subcommand
    p_import = sub.add_parser("import", help="Import Scimago data")
    p_import.add_argument("--file", default=None, help="Path to local .xls/.csv file")
    p_import.add_argument("--url",  default=SCIMAGO_DEFAULT_URL, help="Scimago export URL")
    p_import.add_argument("--year", type=int, default=2024, help="Data year (default: 2024)")
    p_import.add_argument("--limit", type=int, default=None, help="Limit rows for testing")

    # stats subcommand
    sub.add_parser("stats", help="Show database stats")

    args = parser.parse_args()

    if args.command == "import":
        cmd_import(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
