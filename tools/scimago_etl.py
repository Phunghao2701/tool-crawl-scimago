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
    "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db",
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

_name_map = None
_region_map = {
    "Western Europe": ("WE", "WEU"),
    "Latin America": ("LA", "LAC"),
    "Northern America": ("NA", "NAM"),
    "Africa": ("AF", "AFR"),
    "Asiatic Region": ("AS", "ASN"),
    "Eastern Europe": ("EE", "EES"),
    "Middle East": ("ME", "MEA"),
    "Pacific Region": ("PA", "PAC"),
}


def load_countries_map():
    global _name_map
    if _name_map is not None:
        return
    _name_map = {}
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(base_dir, "data", "countries.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for c in data:
                    cca2 = c.get("cca2")
                    cca3 = c.get("cca3")
                    common_name = c.get("name", {}).get("common", "")
                    if common_name:
                        _name_map[common_name.lower()] = (cca2, cca3)
                    official_name = c.get("name", {}).get("official", "")
                    if official_name:
                        _name_map[official_name.lower()] = (cca2, cca3)
        except Exception as e:
            print(f"[warning] Failed to load countries.json: {e}")


def get_zone_codes(name: str, zone_type: str):
    load_countries_map()
    name_clean = name.strip()
    name_lower = name_clean.lower()
    
    if zone_type.upper() == "REGION":
        if name_clean in _region_map:
            return _region_map[name_clean]
        words = name_clean.split()
        if len(words) >= 2:
            code = "".join([w[0].upper() for w in words[:2]])
            iso_code = "".join([w[0].upper() for w in words[:3]]) + "R"
            if len(iso_code) > 3:
                iso_code = iso_code[:3]
        else:
            code = name_clean[:2].upper()
            iso_code = name_clean[:3].upper()
        return code, iso_code
        
    if name_lower in _name_map:
        return _name_map[name_lower]
        
    special_mappings = {
        "united kingdom": ("GB", "GBR"),
        "united states": ("US", "USA"),
        "south korea": ("KR", "KOR"),
        "vietnam": ("VN", "VNM"),
        "viet nam": ("VN", "VNM"),
        "russia": ("RU", "RUS"),
        "russian federation": ("RU", "RUS"),
        "iran": ("IR", "IRN"),
        "iran, islamic republic of": ("IR", "IRN"),
        "syria": ("SY", "SYR"),
        "syrian arab republic": ("SY", "SYR"),
        "laos": ("LA", "LAO"),
        "macao": ("MO", "MAC"),
        "macau": ("MO", "MAC"),
        "taiwan": ("TW", "TWN"),
        "taiwan, province of china": ("TW", "TWN"),
    }
    if name_lower in special_mappings:
        return special_mappings[name_lower]
        
    code = name_clean[:2].upper()
    iso_code = name_clean[:3].upper()
    return code, iso_code


def upsert_zone(conn, name: str, zone_type: str):
    if not name or not name.strip():
        return None
    
    code, iso_code = get_zone_codes(name, zone_type)
    
    row = conn.execute(text("""
        INSERT INTO "Zone" (name, type, code, iso_code, source)
        VALUES (:name, :type, :code, :iso_code, :source)
        ON CONFLICT (name, type) DO UPDATE SET 
            code = EXCLUDED.code,
            iso_code = EXCLUDED.iso_code
        RETURNING zone_id
    """), {
        "name": name.strip(), 
        "type": zone_type.upper(), 
        "code": code, 
        "iso_code": iso_code, 
        "source": "SCIMAGO"
    }).fetchone()
    return row[0]


def upsert_publisher(conn, name: str):
    if not name or not name.strip():
        return None
    row = conn.execute(text("""
        INSERT INTO "Publisher" (display_name)
        VALUES (:name)
        ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING publisher_id
    """), {"name": name.strip()}).fetchone()
    return row[0]


def upsert_metric(conn, code, display_name, metric_type):
    row = conn.execute(text("""
        INSERT INTO "Ranking_Metric" (code, display_name, metric_type)
        VALUES (:code, :display_name, :metric_type)
        ON CONFLICT (code) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            metric_type  = EXCLUDED.metric_type
        RETURNING metric_id
    """), {"code": code, "display_name": display_name, "metric_type": metric_type.upper()}).fetchone()
    return row[0]


def upsert_subject_area(conn, name: str):
    row = conn.execute(text("""
        INSERT INTO "Subject_Area" (display_name)
        VALUES (:name)
        ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING subject_area_id
    """), {"name": name.strip()}).fetchone()
    return row[0]


def upsert_subject_category(conn, area_id, name: str):
    row = conn.execute(text("""
        INSERT INTO "Subject_Category" (subject_area_id, display_name)
        VALUES (:area_id, :name)
        ON CONFLICT (subject_area_id, display_name)
        DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING subject_category_id
    """), {"area_id": area_id, "name": name.strip()}).fetchone()
    return row[0]


def insert_ranking(conn, journal_id, category_id, metric_id, source, year,
                   value_txt=None, value_int=None, value_float=None):
    if value_txt is None and value_int is None and value_float is None:
        return None
    
    v_float = float(value_float) if value_float is not None else None
    
    # Upsert Journal_Ranking và lấy id
    row = conn.execute(text("""
        INSERT INTO "Journal_Ranking"
            (journal_id, subject_category_id, source, metric_id, year, value_txt, value_int, value_float)
        VALUES
            (:jid, :cid, :src, :mid, :yr, :vtxt, :vint, :vfloat)
        ON CONFLICT (journal_id, source, metric_id, year, 
                     (coalesce(value_txt, '')), 
                     (coalesce(value_int, 0)), 
                     (coalesce(value_float, 0)))
        DO UPDATE SET 
            subject_category_id = EXCLUDED.subject_category_id,
            created_at = EXCLUDED.created_at
        RETURNING journal_ranking_id
    """), {
        "jid": journal_id, "cid": category_id, "src": source.upper(), "mid": metric_id, "yr": year,
        "vtxt": value_txt, "vint": value_int, "vfloat": v_float
    }).fetchone()
    
    ranking_id = row[0]
    
    # Chèn liên kết sang Subject Category nếu có chuyên ngành cụ thể
    if category_id:
        conn.execute(text("""
            INSERT INTO "Journal_Ranking_Subject_Category" (journal_ranking_id, subject_category_id)
            VALUES (:rid, :cid)
            ON CONFLICT DO NOTHING
        """), {"rid": ranking_id, "cid": category_id})
    
    return ranking_id


def bulk_insert_raw_sql(conn, table_name, columns, rows, on_conflict_clause=""):
    """
    Thực hiện chèn bulk insert bằng cách sinh câu lệnh SQL thô có dạng:
    INSERT INTO table (col1, col2) VALUES (val1, val2), (val3, val4) ...
    """
    if not rows:
        return
    
    cols_str = ", ".join([f'"{c}"' for c in columns])
    
    values_runs = []
    params = {}
    for i, row in enumerate(rows):
        val_placeholders = []
        for col in columns:
            param_name = f"{col}_{i}"
            val_placeholders.append(f":{param_name}")
            params[param_name] = row.get(col)
        values_runs.append(f"({', '.join(val_placeholders)})")
        
    sql = f"""
        INSERT INTO "{table_name}" ({cols_str})
        VALUES {', '.join(values_runs)}
        {on_conflict_clause}
    """
    conn.execute(text(sql), params)


def bulk_update_journals_sql(conn, rows):
    """
    Thực hiện bulk update cho bảng Journal bằng cách sử dụng UPDATE ... FROM (VALUES ...)
    """
    if not rows:
        return
    
    cols = ["journal_id", "publisher_id", "country", "region", "display_name", "type", "is_open_access", "is_oa_diamond", "issn", "coverage"]
    
    values_runs = []
    params = {}
    for i, row in enumerate(rows):
        val_placeholders = []
        for col in cols:
            param_name = f"{col}_{i}"
            if i == 0:
                if col == "journal_id":
                    val_placeholders.append(f"CAST(:{param_name} AS BIGINT)")
                elif col in ["publisher_id", "country", "region"]:
                    val_placeholders.append(f"CAST(:{param_name} AS BIGINT)")
                elif col in ["is_open_access", "is_oa_diamond"]:
                    val_placeholders.append(f"CAST(:{param_name} AS BOOLEAN)")
                else:
                    val_placeholders.append(f"CAST(:{param_name} AS TEXT)")
            else:
                val_placeholders.append(f":{param_name}")
            params[param_name] = row.get(col)
        values_runs.append(f"({', '.join(val_placeholders)})")
        
    sql = f"""
        UPDATE "Journal" AS j
        SET publisher_id   = tmp.publisher_id,
            country        = tmp.country,
            region         = tmp.region,
            display_name   = tmp.display_name,
            type           = tmp.type,
            is_open_access = tmp.is_open_access,
            is_oa_diamond  = tmp.is_oa_diamond,
            issn           = tmp.issn,
            coverage       = tmp.coverage,
            is_deleted     = false
        FROM (
            VALUES {', '.join(values_runs)}
        ) AS tmp(journal_id, publisher_id, country, region, display_name, type, is_open_access, is_oa_diamond, issn, coverage)
        WHERE j.journal_id = tmp.journal_id
    """
    conn.execute(text(sql), params)


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
        batch_size = 2000
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i+batch_size]
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
            """), batch)
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

        total_rows = len(raw_rows)
        print(f"[normalize] Start processing {total_rows} journals to main database tables...")

        # In-memory caches to reduce database SELECT/INSERT operations dramatically
        print("[normalize] Pre-loading master categories from database...")
        zone_cache = {}
        for row in conn.execute(text('SELECT zone_id, name, type FROM "Zone"')).fetchall():
            zone_cache[(row[1], str(row[2]).lower() if row[2] else "")] = row[0]
            
        publisher_cache = {}
        for row in conn.execute(text('SELECT publisher_id, display_name FROM "Publisher"')).fetchall():
            publisher_cache[row[1]] = row[0]
            
        subject_area_cache = {}
        for row in conn.execute(text('SELECT subject_area_id, display_name FROM "Subject_Area"')).fetchall():
            subject_area_cache[row[1]] = row[0]
            
        subject_category_cache = {}
        for row in conn.execute(text('SELECT subject_category_id, subject_area_id, display_name FROM "Subject_Category"')).fetchall():
            subject_category_cache[(row[1], row[2])] = row[0]

        # Pre-load all existing Journals into memory
        print("[normalize] Pre-loading existing journals from database...")
        existing_journals = conn.execute(text('SELECT journal_id, source_id, issn FROM "Journal"')).fetchall()
        src_to_jid = {}
        issn_to_jid = {}
        for row in existing_journals:
            jid, src_id, issn_val = row[0], row[1], row[2]
            if src_id:
                src_to_jid[src_id] = jid
                src_to_jid[f"S_NOT_FOUND_{src_id}"] = jid
                src_to_jid[f"S_NO_ISSN_{src_id}"] = jid
                src_to_jid[f"S_DUPLICATE_OPENALEX_{src_id}"] = jid
            if issn_val:
                parts = [x.strip() for x in issn_val.replace(",", " ").split() if x.strip()]
                for part in parts:
                    if len(part) >= 8:
                        issn_to_jid[part] = jid

        journals_to_insert = []
        journals_to_update = []

        print("[normalize] Pass 1: Resolving references and preparing journal data...")
        # PASS 1: Chuẩn bị dữ liệu Journal để bulk insert/update
        for raw in raw_rows:
            src_id = (raw["source_id"] or "").strip()
            title  = (raw["title"] or "").strip()
            if not src_id or not title:
                continue

            # Zone upserts with cache
            country_val = raw["country"]
            country_key = (country_val, "country")
            if country_key not in zone_cache:
                zone_cache[country_key] = upsert_zone(conn, country_val, "country")
            country_id = zone_cache[country_key]

            region_val = raw["region"]
            region_key = (region_val, "region")
            if region_key not in zone_cache:
                zone_cache[region_key] = upsert_zone(conn, region_val, "region")
            region_id = zone_cache[region_key]

            # Publisher upsert with cache
            pub_val = raw["publisher"]
            if pub_val not in publisher_cache:
                publisher_cache[pub_val] = upsert_publisher(conn, pub_val)
            pub_id = publisher_cache[pub_val]

            # Tra cứu journal_id từ cache bộ nhớ
            existing_jid = None
            if src_id in src_to_jid:
                existing_jid = src_to_jid[src_id]
            else:
                issn_val = (raw["issn"] or "").strip() or None
                if issn_val:
                    issn_parts = [x.strip() for x in issn_val.replace(",", " ").split() if x.strip()]
                    for part in issn_parts:
                        if len(part) >= 8 and part in issn_to_jid:
                            existing_jid = issn_to_jid[part]
                            break

            raw_data = raw["raw_json"] if raw["raw_json"] else {}
            if isinstance(raw_data, str):
                raw_data = json.loads(raw_data)
            coverage_val = raw_data.get("Coverage", None)
            coverage_str = (str(coverage_val) if coverage_val is not None else "").strip() or None
            issn_val = (raw["issn"] or "").strip() or None

            journal_item = {
                "source_id":       src_id,
                "publisher_id":    pub_id,
                "country":         country_id,
                "region":          region_id,
                "display_name":    title,
                "type":            (raw["type"] or "").strip() or None,
                "is_open_access":  norm_bool(raw["open_access"]),
                "is_oa_diamond":   norm_bool(raw["open_access_diamond"]),
                "issn":            issn_val,
                "coverage":        coverage_str
            }

            if existing_jid:
                journal_item["journal_id"] = existing_jid
                journals_to_update.append(journal_item)
            else:
                journals_to_insert.append(journal_item)

        # Thực thi Bulk Insert cho Journal mới
        if journals_to_insert:
            print(f"[normalize] Inserting {len(journals_to_insert)} new journals...")
            journal_cols = ["source_id", "publisher_id", "country", "region", "display_name", "type", "is_open_access", "is_oa_diamond", "issn", "coverage"]
            batch_size = 2000
            for i in range(0, len(journals_to_insert), batch_size):
                batch = journals_to_insert[i:i+batch_size]
                bulk_insert_raw_sql(conn, "Journal", journal_cols, batch)

        # Thực thi Bulk Update cho Journal cũ
        if journals_to_update:
            print(f"[normalize] Updating {len(journals_to_update)} existing journals...")
            batch_size = 1000
            for i in range(0, len(journals_to_update), batch_size):
                batch = journals_to_update[i:i+batch_size]
                bulk_update_journals_sql(conn, batch)

        # Sau khi hoàn tất Pass 1, load lại toàn bộ danh sách Journal để có journal_id đầy đủ trong cache bộ nhớ
        print("[normalize] Reloading journal cache for Pass 2...")
        existing_journals = conn.execute(text('SELECT journal_id, source_id, issn FROM "Journal"')).fetchall()
        src_to_jid = {}
        issn_to_jid = {}
        for row in existing_journals:
            jid, src_id, issn_val = row[0], row[1], row[2]
            if src_id:
                src_to_jid[src_id] = jid
                src_to_jid[f"S_NOT_FOUND_{src_id}"] = jid
                src_to_jid[f"S_NO_ISSN_{src_id}"] = jid
                src_to_jid[f"S_DUPLICATE_OPENALEX_{src_id}"] = jid
            if issn_val:
                parts = [x.strip() for x in issn_val.replace(",", " ").split() if x.strip()]
                for part in parts:
                    if len(part) >= 8:
                        issn_to_jid[part] = jid

        # PASS 2: Thu thập thông tin link Subject Category và Rankings
        print("[normalize] Pass 2: Building subject categories and ranking metrics...")
        subject_links_to_insert = []
        rankings_to_insert = []

        ok = skipped = 0
        for raw in raw_rows:
            src_id = (raw["source_id"] or "").strip()
            title  = (raw["title"] or "").strip()
            if not src_id or not title:
                skipped += 1
                continue

            # Lấy jid từ cache bộ nhớ
            jid = None
            if src_id in src_to_jid:
                jid = src_to_jid[src_id]
            else:
                issn_val = (raw["issn"] or "").strip() or None
                if issn_val:
                    issn_parts = [x.strip() for x in issn_val.replace(",", " ").split() if x.strip()]
                    for part in issn_parts:
                        if len(part) >= 8 and part in issn_to_jid:
                            jid = issn_to_jid[part]
                            break
            
            if not jid:
                skipped += 1
                continue

            # Subject categories
            categories = parse_categories(raw["categories"])
            quartiles_all = []
            for cat in categories:
                area_name = (raw.get("areas") or "").strip() or "General"
                if area_name not in subject_area_cache:
                    subject_area_cache[area_name] = upsert_subject_area(conn, area_name)
                area_id = subject_area_cache[area_name]

                cat_name = cat["name"]
                cat_key = (area_id, cat_name)
                if cat_key not in subject_category_cache:
                    subject_category_cache[cat_key] = upsert_subject_category(conn, area_id, cat_name)
                cat_id = subject_category_cache[cat_key]

                # Link category
                subject_links_to_insert.append({"journal_id": jid, "subject_category_id": cat_id})

                # quartile per category
                if cat["quartile"]:
                    rankings_to_insert.append({
                        "journal_id": jid, "subject_category_id": cat_id, "source": "SCIMAGO", "metric_id": metrics["SJR_QUARTILE_BY_CAT"], "year": year,
                        "value_txt": cat["quartile"], "value_int": None, "value_float": None
                    })
                    quartiles_all.append(cat["quartile"])

            # Best quartile
            bq = best_quartile(quartiles_all)
            if bq:
                rankings_to_insert.append({
                    "journal_id": jid, "subject_category_id": None, "source": "SCIMAGO", "metric_id": metrics["SJR_BEST_QUARTILE"], "year": year,
                    "value_txt": bq, "value_int": None, "value_float": None
                })

            # Numeric rankings
            def add_num_rank(metric_key, raw_val, val_type):
                val = raw.get(raw_val)
                if val is not None and str(val).strip():
                    v_int = norm_int(val) if val_type == "int" else None
                    v_float = float(norm_decimal(val)) if val_type == "float" else None
                    if v_int is not None or v_float is not None:
                        rankings_to_insert.append({
                            "journal_id": jid, "subject_category_id": None, "source": "SCIMAGO", "metric_id": metrics[metric_key], "year": year,
                            "value_txt": None, "value_int": v_int, "value_float": v_float
                        })

            add_num_rank("RANK", "rank_txt", "int")
            add_num_rank("SJR", "sjr", "float")
            add_num_rank("H_INDEX", "h_index", "int")
            add_num_rank("TOTAL_DOCS_CURRENT_YEAR", "total_docs_current_year", "int")
            add_num_rank("TOTAL_DOCS_3YEARS", "total_docs_3years", "int")
            add_num_rank("TOTAL_REFS", "total_refs", "int")
            add_num_rank("TOTAL_CITES_3YEARS", "total_cites_3years", "int")
            add_num_rank("CITABLE_DOCS_3YEARS", "citable_docs_3years", "int")
            add_num_rank("CITES_PER_DOC_2YEARS", "cites_doc_2years", "float")
            add_num_rank("REF_PER_DOC", "ref_doc", "float")

            ok += 1
            if ok % 5000 == 0 or ok == total_rows:
                print(f"[normalize] Prepared {ok}/{total_rows} journals ({(ok * 100) // total_rows}%)")

        # Thực thi Bulk Insert cho Journal_Subject_Category
        if subject_links_to_insert:
            print(f"[normalize] Linking {len(subject_links_to_insert)} journals with categories...")
            batch_size = 2000
            for i in range(0, len(subject_links_to_insert), batch_size):
                batch = subject_links_to_insert[i:i+batch_size]
                bulk_insert_raw_sql(conn, "Journal_Subject_Category", ["journal_id", "subject_category_id"], batch, "ON CONFLICT DO NOTHING")

        # Thực thi Bulk Insert cho Journal_Ranking
        if rankings_to_insert:
            # Lọc trùng để tránh CardinalityViolation trong bulk operation
            unique_rankings = []
            seen_ranking_keys = set()
            for r in rankings_to_insert:
                key = (
                    r["journal_id"],
                    r["source"],
                    r["metric_id"],
                    r["year"],
                    r["value_txt"] or "",
                    r["value_int"] or 0,
                    r["value_float"] or 0.0
                )
                if key not in seen_ranking_keys:
                    seen_ranking_keys.add(key)
                    unique_rankings.append(r)
            rankings_to_insert = unique_rankings

            print(f"[normalize] Inserting {len(rankings_to_insert)} journal rankings...")
            on_conflict = """
                ON CONFLICT (journal_id, source, metric_id, year, 
                             (coalesce(value_txt, ''::character varying)), 
                             (coalesce(value_int, 0)), 
                             (coalesce(value_float, (0)::double precision)))
                DO UPDATE SET 
                    subject_category_id = EXCLUDED.subject_category_id,
                    created_at = EXCLUDED.created_at
            """
            batch_size = 1000
            for i in range(0, len(rankings_to_insert), batch_size):
                batch = rankings_to_insert[i:i+batch_size]
                bulk_insert_raw_sql(conn, "Journal_Ranking", ["journal_id", "subject_category_id", "source", "metric_id", "year", "value_txt", "value_int", "value_float"], batch, on_conflict)

            # Thực thi Bulk Insert cho Journal_Ranking_Subject_Category chỉ trong 1 câu SQL duy nhất!
            print("[normalize] Linking journal rankings with categories...")
            conn.execute(text("""
                INSERT INTO "Journal_Ranking_Subject_Category" (journal_ranking_id, subject_category_id)
                SELECT jr.journal_ranking_id, jr.subject_category_id
                FROM "Journal_Ranking" jr
                WHERE jr.source = 'SCIMAGO' AND jr.year = :year AND jr.subject_category_id IS NOT NULL
                ON CONFLICT DO NOTHING
            """), {"year": year})

        print(f"[normalize] Completed! ok={ok}, skipped={skipped}")


# ---------------------------------------------------------------
# Commands
# ---------------------------------------------------------------

def cmd_import(args):
    engine = create_engine(DATABASE_URL)

    if args.file:
        file_path = args.file
        # Nếu file không tồn tại ở thư mục làm việc hiện tại và là đường dẫn tương đối,
        # tự động tìm trong thư mục gốc dự án.
        if not os.path.exists(file_path) and not os.path.isabs(file_path):
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            possible_path = os.path.join(project_root, file_path)
            if os.path.exists(possible_path):
                file_path = possible_path
                
        with open(file_path, "rb") as f:
            content = f.read()
        print(f"[import] file={file_path}")
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
            "Publisher", "Zone", "Journal",
            "Subject_Area", "Subject_Category", "Journal_Subject_Category",
            "Ranking_Metric", "Journal_Ranking",
        ]
        print("\n[DB] Database stats:")
        print(f"{'Table':<35} {'Rows':>8}")
        print("-" * 45)
        for t in tables:
            try:
                t_escaped = f'"{t}"' if t[0].isupper() else t
                n = conn.execute(text(f"SELECT COUNT(*) FROM {t_escaped}")).scalar()
                print(f"  {t:<33} {n:>8,}")
            except Exception as exc:
                print(f"  {t:<33} ERROR: {exc}")

        print("\n[DB] Sample journals:")
        rows = conn.execute(text("""
            SELECT j.source_id, j.display_name, j.type,
                   j.is_open_access, z.name AS country
            FROM "Journal" j
            LEFT JOIN "Zone" z ON z.zone_id = j.country
            ORDER BY j.journal_id
            LIMIT 10
        """)).fetchall()
        for r in rows:
            oa = "OA" if r.is_open_access else "  "
            print(f"  [{oa}] {r.source_id:>10}  {r.display_name[:55]:<55}  {r.country or '':>20}")

        print("\n[DB] Sample rankings (SJR):")
        rows = conn.execute(text("""
            SELECT j.display_name, jr.year, jr.value_float
            FROM "Journal_Ranking" jr
            JOIN "Journal" j        ON j.journal_id = jr.journal_id
            JOIN "Ranking_Metric" m ON m.metric_id  = jr.metric_id
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
