"""
OpenAlex Sync Tool
Usage:
  python tools/openalex_sync.py sync --limit 10
  python tools/openalex_sync.py stats
"""
import urllib3
import argparse
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from datetime import datetime, timezone
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from psycopg2.extras import execute_values

from pipeline_lock import acquire as acquire_lock
# Lấy đường dẫn tuyệt đối tới file .env nằm ở thư mục gốc của project
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path, override=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres123@localhost:5432/researchpulse",
)
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "academic-etl@example.com")
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY")
print("[INFO] Email OpenAlex dang su dung:", OPENALEX_EMAIL)
if OPENALEX_API_KEY:
    print("[INFO] Da tim thay OPENALEX_API_KEY trong file .env.")
else:
    print("[WARNING] KHONG tim thay OPENALEX_API_KEY trong file .env!")
    print("  -> Vui long dang ky tai khoan mien phi tai https://openalex.org/settings/api")
    print("  -> Lay API Key mien phi va them vao file .env de tranh loi HTTP 429.")


def get_headers():
    # Sử dụng Polite Pool của OpenAlex theo khuyến nghị chính thức
    return {
        "User-Agent": f"ScientificJournalETL/1.0 (mailto:{OPENALEX_EMAIL})"
    }


def check_insufficient_budget(resp):
    if resp.status_code == 429:
        try:
            err_data = resp.json()
            if "Insufficient budget" in err_data.get("message", "") or "pricing" in err_data.get("message", ""):
                print("\n" + "="*80)
                print("[CRITICAL] OPENALEX API ERROR: INSUFFICIENT BUDGET (HẾT NGÂN SÁCH MIỄN PHÍ)")
                print("="*80)
                print("Chi tiết lỗi từ OpenAlex:")
                print(f"  {err_data.get('message')}")
                print("\nCách khắc phục nhanh:")
                print("  1. Mở file .env và XÓA (hoặc để trống) dòng OPENALEX_API_KEY.")
                print("     (Để trống để sử dụng chế độ Polite Pool miễn phí thay vì tài khoản tính phí)")
                print("  2. THAY ĐỔI ĐỊA CHỈ IP MẠNG CỦA BẠN (bằng cách kết nối VPN hoặc phát 4G từ điện thoại).")
                print("     (Bắt buộc phải đổi IP vì OpenAlex đã cache IP của bạn gắn liền với tài khoản hết tiền kia)")
                print("  3. Sau đó chạy lại pipeline.")
                print("="*80 + "\n")
                sys.exit(1)
        except Exception:
            pass


_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Mot requests.Session ben vung cho moi thread, tai su dung ket noi (keep-alive)
    thay vi mo socket TCP moi cho tung request - tranh can kiet socket buffer khi
    chay nhieu thread song song (WinError 10055 tren Windows)."""
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return _thread_local.session


def safe_get(url, headers=None, timeout=15):
    import urllib3
    import urllib.parse as urllib_parse
    # Tắt các cảnh báo không an toàn khi dùng verify=False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # Tự động gắn mailto và api_key vào query parameters để bảo đảm vào Polite Pool và xác thực tài khoản
    parsed_url = urllib_parse.urlparse(url)
    query_params = urllib_parse.parse_qs(parsed_url.query)
    
    modified = False
    if OPENALEX_EMAIL and 'mailto' not in query_params:
        query_params['mailto'] = [OPENALEX_EMAIL]
        modified = True
    if OPENALEX_API_KEY and 'api_key' not in query_params:
        query_params['api_key'] = [OPENALEX_API_KEY]
        modified = True
        
    if modified:
        new_query = urllib_parse.urlencode(query_params, doseq=True)
        url = parsed_url._replace(query=new_query).geturl()
            
    if headers is None:
        headers = get_headers()
        
    session = _get_session()
    retries = 3
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                check_insufficient_budget(resp)
                print(f"  [API Limit] HTTP 429 (Too Many Requests). Sleeping 10s before retry {attempt+1}/{retries}...")
                time.sleep(10)
                continue
            return resp
        except Exception as e:
            # Bắt các lỗi kết nối/SSL (vd WinError 10055 khi socket buffer can kiet):
            # nghi 1 chut roi thu lai tren cung session thay vi mo socket moi ngay lap tuc.
            print(f"  [Connection Warning] Attempt {attempt+1}/{retries} failed: {e}.")
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                # Trả về một đối tượng giả lập để tránh làm sập chương trình
                class FakeResponse:
                    status_code = 500
                    def json(self): return {}
                return FakeResponse()
                    
    # Hết lượt retry và vẫn bị 429
    class FakeResponse429:
        status_code = 429
        def json(self): return {}
    return FakeResponse429()


_subject_area_cache = {}
_subject_category_cache = {}

def get_or_create_subject_info(conn, field_name, subfield_name):
    global _subject_area_cache, _subject_category_cache
    subject_area_id = None
    subject_category_id = None
    
    if field_name:
        if field_name in _subject_area_cache:
            subject_area_id = _subject_area_cache[field_name]
        else:
            subject_area_id = conn.execute(text("""
                INSERT INTO "Subject_Area" (display_name)
                VALUES (:name)
                ON CONFLICT (display_name) DO UPDATE
                    SET display_name = EXCLUDED.display_name
                RETURNING subject_area_id
            """), {"name": field_name}).scalar()
            _subject_area_cache[field_name] = subject_area_id
            
    if subfield_name:
        cat_key = (subject_area_id, subfield_name)
        if cat_key in _subject_category_cache:
            subject_category_id = _subject_category_cache[cat_key]
        else:
            subject_category_id = conn.execute(text("""
                INSERT INTO "Subject_Category" (subject_area_id, display_name)
                VALUES (:area_id, :name)
                ON CONFLICT (subject_area_id, display_name) DO UPDATE
                    SET display_name = EXCLUDED.display_name
                RETURNING subject_category_id
            """), {"area_id": subject_area_id, "name": subfield_name}).scalar()
            _subject_category_cache[cat_key] = subject_category_id
            
    return subject_area_id, subject_category_id


def get_or_create_topic(conn, name, score, field_name, subfield_name):
    subject_area_id, subject_category_id = get_or_create_subject_info(
        conn, field_name, subfield_name
    )
    return conn.execute(text("""
        INSERT INTO "Topic" (
            display_name, score, subject_area_id, subject_category_id
        )
        VALUES (:name, :score, :area_id, :category_id)
        ON CONFLICT (display_name) DO UPDATE SET
            subject_area_id = COALESCE("Topic".subject_area_id, EXCLUDED.subject_area_id),
            subject_category_id = COALESCE("Topic".subject_category_id, EXCLUDED.subject_category_id),
            score = EXCLUDED.score
        RETURNING topic_id
    """), {
        "name": name,
        "score": score,
        "area_id": subject_area_id,
        "category_id": subject_category_id,
    }).scalar()


def get_or_create_keyword(conn, name):
    return conn.execute(text("""
        INSERT INTO "Keyword" (display_name)
        VALUES (:name)
        ON CONFLICT (display_name) DO UPDATE SET
            display_name = EXCLUDED.display_name
        RETURNING keyword_id
    """), {"name": name}).scalar()


def split_issns(v: str):
    if not v:
        return []
    import re
    parts = re.split(r"[,;/\s]+", v)
    cleaned = []
    for p in parts:
        p = p.strip().replace("-", "").upper()
        if re.match(r"^\d{7}[\dX]$", p):
            cleaned.append(p)
    return cleaned




def sync_journals(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Truy vấn các journal chưa đồng bộ OpenAlex trực tiếp từ bảng "Journal"
    # Sắp xếp ưu tiên các tạp chí có Rank tốt nhất (rank từ 1 trở đi) lên đầu
    query = """
        SELECT j.journal_id, j.display_name, j.issn, j.source_id
        FROM "Journal" j
        WHERE j.source_id NOT LIKE 'https://openalex.org/%' AND j.source_id NOT LIKE 'S%' AND j.is_deleted = false
        ORDER BY j.journal_id ASC
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        journals = conn.execute(text(query)).fetchall()

    if not journals:
        print("[INFO] No journals need synchronization.")
        return

    print(f"[sync] Starting bulk synchronization for {len(journals)} journals...")
    
    # Xây dựng bản đồ mapping ISSN -> journal info
    issn_to_journals = {}
    journal_issns = {} # journal_id -> list of clean issns
    journal_info = {}  # journal_id -> (display_name, source_id)
    
    for journal in journals:
        journal_id = journal[0]
        display_name = journal[1]
        issn_str = journal[2] or ""
        source_id = journal[3]
        
        issns = split_issns(issn_str)
        journal_info[journal_id] = (display_name, source_id)
        if not issns:
            # Đánh dấu những journal không có ISSN là đã sync (thất bại) để bỏ qua lần sau
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE "Journal"
                    SET source_id = :failed_source_id
                    WHERE journal_id = :journal_id
                """), {
                    "failed_source_id": f"S_NO_ISSN_{source_id}" if source_id else "S_NO_ISSN",
                    "journal_id": journal_id
                })
            continue
            
        journal_issns[journal_id] = issns
        for issn in issns:
            if issn not in issn_to_journals:
                issn_to_journals[issn] = []
            issn_to_journals[issn].append(journal_id)
            
    # Lấy danh sách tất cả các ISSN cần quét
    all_issns = list(issn_to_journals.keys())
    if not all_issns:
        print("[INFO] No valid ISSNs to query on OpenAlex.")
        return
        
    print(f"[sync] Prepared {len(all_issns)} unique ISSNs to query in chunks of 50...")
    
    # Lấy tất cả OpenAlex ID (được lưu trong source_id) đã tồn tại trong DB để tránh UniqueViolation
    with engine.connect() as conn:
        existing_rows = conn.execute(text("SELECT source_id FROM \"Journal\" WHERE source_id LIKE 'https://openalex.org/%' OR source_id LIKE 'S%'")).fetchall()
    existing_openalex_ids = {row[0] for row in existing_rows}
    synced_openalex_ids_this_run = set()
    
    synced_journal_ids = set()
    synced_count = 0
    failed_count = 0
    
    # Chia nhỏ thành các chunk size 50
    chunk_size = 50
    for i in range(0, len(all_issns), chunk_size):
        chunk = all_issns[i:i+chunk_size]
        print(f"\n[sync] Processing chunk {i//chunk_size + 1}/{(len(all_issns)-1)//chunk_size + 1} ({len(chunk)} ISSNs)...")
        
        # Tạo filter OR
        formatted_issns = []
        for issn in chunk:
            if len(issn) == 8:
                formatted_issns.append(f"{issn[:4]}-{issn[4:]}")
            else:
                formatted_issns.append(issn)
                
        filter_str = "|".join(formatted_issns)
        url = f"https://api.openalex.org/sources?filter=issn:{filter_str}&per_page=100"
        
        # Thử gọi API (safe_get có cơ chế retry 3 lần)
        time.sleep(0.5) # Lịch sự tránh rate limit
        resp = safe_get(url, timeout=20)
        
        if resp.status_code != 200:
            print(f"  [Error] Failed to fetch chunk from OpenAlex. HTTP {resp.status_code}")
            continue
            
        data = resp.json()
        results = data.get("results", [])
        print(f"  -> OpenAlex returned {len(results)} sources matching filter.")
        
        # Tập hợp các journal_id tìm thấy trong chunk này
        chunk_synced_ids = set()
        
        for source_data in results:
            openalex_id = source_data.get("id")
            works_count = source_data.get("works_count")
            cited_by_count = source_data.get("cited_by_count")
            publisher_name = source_data.get("publisher")
            
            # Lấy tất cả các ISSN mà OpenAlex ghi nhận cho source này (trường "issn" và "issn_l")
            source_issns = source_data.get("issn", [])
            if not isinstance(source_issns, list):
                source_issns = [source_issns] if source_issns else []
            issn_l = source_data.get("issn_l")
            if issn_l and issn_l not in source_issns:
                source_issns.append(issn_l)
                
            # Chuẩn hóa để đối chiếu
            clean_source_issns = []
            for s_issn in source_issns:
                clean_s = s_issn.replace("-", "").upper()
                clean_source_issns.append(clean_s)
                
            # Thử map với journal trong DB của chúng ta thông qua các ISSN này
            matched_journal_ids = set()
            for s_issn in clean_source_issns:
                if s_issn in issn_to_journals:
                    for j_id in issn_to_journals[s_issn]:
                        matched_journal_ids.add(j_id)
                        
            # Cập nhật từng journal tìm thấy
            for j_id in matched_journal_ids:
                if j_id in synced_journal_ids:
                    continue
                    
                display_name = journal_info[j_id][0]
                
                # Tránh UniqueViolation nếu openalex_id đã được gán cho journal khác
                if openalex_id in existing_openalex_ids or openalex_id in synced_openalex_ids_this_run:
                    print(f"  -> WARNING: OpenAlex ID {openalex_id} already mapped in DB. Marking '{display_name}' as synced without setting openalex_id to prevent UniqueViolation.")
                    source_id = journal_info[j_id][1]
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE "Journal"
                            SET source_id = :failed_source_id
                            WHERE journal_id = :journal_id
                        """), {
                            "failed_source_id": f"S_DUPLICATE_OPENALEX_{source_id}" if source_id else "S_DUPLICATE_OPENALEX",
                            "journal_id": j_id
                        })
                    synced_journal_ids.add(j_id)
                    chunk_synced_ids.add(j_id)
                    synced_count += 1
                    continue

                # Đồng bộ Publisher
                publisher_uuid = None
                if publisher_name:
                    with engine.begin() as conn:
                        pub_row = conn.execute(text("""
                            SELECT publisher_id FROM "Publisher" WHERE display_name = :name
                        """), {"name": publisher_name}).fetchone()
                        if pub_row:
                            publisher_uuid = pub_row[0]
                        else:
                            publisher_uuid = conn.execute(text("""
                                INSERT INTO "Publisher" (display_name)
                                VALUES (:name)
                                RETURNING publisher_id
                            """), {"name": publisher_name}).scalar()
                            
                # Cập nhật Journal
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE "Journal"
                        SET source_id = :openalex_id,
                            publisher_id = COALESCE(:publisher_uuid, publisher_id)
                        WHERE journal_id = :journal_id
                    """), {
                        "openalex_id": openalex_id,
                        "publisher_uuid": publisher_uuid,
                        "journal_id": j_id
                    })
                
                print(f"  -> SUCCESS: '{display_name}' mapped to OpenAlex ID={openalex_id}")
                synced_journal_ids.add(j_id)
                chunk_synced_ids.add(j_id)
                synced_openalex_ids_this_run.add(openalex_id)
                synced_count += 1
                
        # Với những journal thuộc chunk này nhưng KHÔNG tìm thấy trên OpenAlex
        # Ta duyệt qua các journal có ISSN nằm trong chunk này mà chưa được đánh dấu sync
        for issn in chunk:
            for j_id in issn_to_journals[issn]:
                if j_id not in synced_journal_ids and j_id not in chunk_synced_ids:
                    # Đánh dấu đã quét (thất bại) để tránh lặp lần sau
                    display_name = journal_info[j_id][0]
                    source_id = journal_info[j_id][1]
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE "Journal"
                            SET source_id = :failed_source_id
                            WHERE journal_id = :journal_id
                        """), {
                            "failed_source_id": f"S_NOT_FOUND_{source_id}" if source_id else "S_NOT_FOUND",
                            "journal_id": j_id
                        })
                    print(f"  -> NOT FOUND: '{display_name}' (ISSN {issn}) not found on OpenAlex.")
                    synced_journal_ids.add(j_id)
                    failed_count += 1

    print(f"\n[sync] Finished! Synced: {synced_count}, Failed/Not found: {failed_count}")


def cmd_stats(args):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text('SELECT COUNT(*) FROM "Journal" WHERE is_deleted = false')).scalar()
        synced = conn.execute(text("SELECT COUNT(*) FROM \"Journal\" WHERE (source_id LIKE 'https://openalex.org/%' OR source_id LIKE 'S%') AND is_deleted = false")).scalar()
        unsynced = total - synced
        
        print("\n[OpenAlex Sync Stats]")
        print(f"  Total journals in DB:    {total:,}")
        pct = (synced / total * 100) if total > 0 else 0.0
        print(f"  Synced with OpenAlex:    {synced:,} ({pct:.1f}%)")
        print(f"  Pending sync:            {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Journals Sample]")
            rows = conn.execute(text("""
                SELECT display_name, source_id, created_at
                FROM "Journal"
                WHERE (source_id LIKE 'https://openalex.org/%' OR source_id LIKE 'S%') AND is_deleted = false
                ORDER BY created_at DESC NULLS LAST
                LIMIT 10
            """)).fetchall()
            
            print(f"  {'Journal Name':<50} {'Created At':<25} {'OpenAlex ID':<25}")
            print("  " + "-" * 104)
            for r in rows:
                name = r[0][:47] + "..." if len(r[0]) > 50 else r[0]
                synced_at = str(r[2]) if r[2] is not None else "Not yet"
                oid = r[1].replace("https://openalex.org/", "") if r[1] else "N/A"
                print(f"  {name:<50} {synced_at:<25} {oid:<25}")


def cmd_export(args):
    import pandas as pd
    import json
    engine = create_engine(DATABASE_URL)
    
    print("[export] Fetching journals and raw scimago data from PostgreSQL...")
    
    try:
        # This export no longer depends on the removed raw_scimago_journal staging table.
        with engine.connect() as conn:
            journals = conn.execute(text("""
                SELECT journal_id, source_id, issn, display_name, created_at
                FROM "Journal"
                WHERE is_deleted = false
            """)).fetchall()
            
        print(f"[export] Loaded {len(journals)} journals.")
        
        records = []
        
        for j in journals:
            raw_dict = {
                "Sourceid": j.source_id,
                "Title": j.display_name,
                "Issn": j.issn,
            }
                
            # Gắn thêm cột OpenAlex làm giàu thông tin
            oa_id = j.source_id if (j.source_id and ('openalex.org' in j.source_id or j.source_id.startswith('S'))) else None
            raw_dict["OpenAlex ID"] = oa_id
            raw_dict["OpenAlex Works Count"] = None
            raw_dict["OpenAlex Cited By Count"] = None
            
            records.append(raw_dict)
            
        print(f"[export] Prepared {len(journals)} journal records for export.")
            
        if not records:
            print("[export] No data found in database to export.")
            return
            
        df = pd.DataFrame(records)
        
        # Sort dynamically by SJR descending if SJR column exists
        sjr_col = None
        for col in df.columns:
            if col.lower() == "sjr":
                sjr_col = col
                break
        if sjr_col:
            df_temp_sjr = pd.to_numeric(df[sjr_col].astype(str).str.replace(",", ".", regex=False), errors='coerce')
            df = df.iloc[df_temp_sjr.sort_values(ascending=False).index]
        
        # Sắp xếp các cột theo thứ tự khoa học và logic (Định danh -> Chỉ số -> Open Access -> OpenAlex -> Chi tiết)
        actual_cols = list(df.columns)
        priority_rules = [
            lambda c: c.lower() == "rank",
            lambda c: c.lower() == "sourceid",
            lambda c: c.lower() == "title",
            lambda c: c.lower() == "type",
            lambda c: c.lower() == "issn",
            lambda c: c.lower() == "publisher",
            lambda c: c.lower() == "country",
            lambda c: c.lower() == "region",
            lambda c: c.lower() == "coverage",
            lambda c: c.lower() == "sjr",
            lambda c: c.lower() == "sjr best quartile",
            lambda c: c.lower() == "h index",
            lambda c: c.lower() == "open access",
            lambda c: c.lower() == "open access diamond",
            lambda c: c.lower() == "openalex id",
            lambda c: c.lower() == "openalex homepage",
            lambda c: c.lower() == "openalex works count",
            lambda c: c.lower() == "openalex cited by count",
            lambda c: c.lower() == "scope detail",
            lambda c: c.lower().startswith("total docs. (") or c.lower().startswith("total docs ("),
            lambda c: "total docs" in c.lower() and "3years" in c.lower().replace(" ", ""),
            lambda c: "total refs" in c.lower(),
            lambda c: "total citations" in c.lower() or "total cites" in c.lower(),
            lambda c: "citable docs" in c.lower(),
            lambda c: "citations" in c.lower() and "doc" in c.lower(),
            lambda c: "ref" in c.lower() and "doc" in c.lower(),
            lambda c: "%female" in c.lower() or "female" in c.lower(),
            lambda c: "overton" in c.lower(),
            lambda c: "areas" in c.lower(),
            lambda c: "categories" in c.lower(),
        ]
        
        ordered_cols = []
        # Lấy các cột theo thứ tự ưu tiên
        for rule in priority_rules:
            matched = [c for c in actual_cols if rule(c) and c not in ordered_cols]
            ordered_cols.extend(matched)
            
        # Đưa các cột còn lại xuống cuối để đảm bảo xuất ra 100% cột thô
        remaining = [c for c in actual_cols if c not in ordered_cols]
        ordered_cols.extend(remaining)
        
        df = df[ordered_cols]
        
        # Select preview columns dynamically
        cols_to_print = []
        for target in ["title", "issn", "publisher", "sjr", "openalex id", "openalex works count"]:
            for col in df.columns:
                if col.lower() == target:
                    cols_to_print.append(col)
                    break
        if not cols_to_print:
            cols_to_print = list(df.columns[:8])

            
        # Print preview sample
        limit = args.limit or 10
        print(f"\n[export] Enriched Journals Preview (Top {limit}):")
        print(df[cols_to_print].head(limit).to_string(index=False))
        
        # Save to CSV
        output_file = args.output
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n[OK] Exported {len(df)} enriched records to CSV: {output_file}")
        
        # Save to Excel (.xlsx)
        excel_file = os.path.splitext(output_file)[0] + ".xlsx"
        try:
            df.to_excel(excel_file, index=False)
            print(f"[OK] Exported {len(df)} enriched records to Excel: {excel_file}")
        except (ImportError, ModuleNotFoundError):
            print("[INFO] Installing openpyxl for Excel output...")
            try:
                import subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl"])
                df.to_excel(excel_file, index=False)
                print(f"[OK] Exported {len(df)} enriched records to Excel: {excel_file}")
            except Exception as e_install:
                print(f"[WARNING] Failed to install openpyxl or save Excel: {e_install}")
        except Exception as e_excel:
            print(f"[WARNING] Could not save Excel file: {e_excel}")
            
    except Exception as e:
        print(f"[ERROR] Failed to export data: {e}")


def update_author_in_db(engine, author_id, data):
    openalex_id = data.get("id")
    orcid = data.get("orcid")
    disp_name = data.get("display_name")
    works_count = data.get("works_count")
    cited_by_count = data.get("cited_by_count")
    
    summary_stats = data.get("summary_stats") or {}
    h_index = summary_stats.get("h_index")
    i10_index = summary_stats.get("i10_index")
    
    last_inst_list = data.get("last_known_institutions") or []
    last_inst_name = None
    last_inst_id = None
    if last_inst_list:
        last_inst_name = last_inst_list[0].get("display_name")
        last_inst_id = last_inst_list[0].get("id")
        

    def execute_update(
        preserve_existing_orcid=False,
        preserve_existing_openalex_id=False,
    ):
        with engine.begin() as conn:
            conn.execute(text("""
            UPDATE "Author"
            SET openalex_id = CASE
                    WHEN :preserve_existing_openalex_id THEN openalex_id
                    ELSE :openalex_id
                END,
                orcid = CASE
                    WHEN :preserve_existing_orcid THEN orcid
                    ELSE :orcid
                END,
                display_name = COALESCE(:disp_name, display_name),
                works_count = :works_count,
                cited_by_count = :cited_by_count,
                h_index = :h_index,
                i10_index = :i10_index,
                last_known_institution = :last_inst_name,
                last_known_institution_id = :last_inst_id,
                created_at = :synced_at
            WHERE author_id = :author_id
        """), {
                "openalex_id": openalex_id,
                "preserve_existing_openalex_id": preserve_existing_openalex_id,
                "orcid": orcid,
                "preserve_existing_orcid": preserve_existing_orcid,
                "disp_name": disp_name,
                "works_count": works_count,
                "cited_by_count": cited_by_count,
                "h_index": h_index,
                "i10_index": i10_index,
                "last_inst_name": last_inst_name,
                "last_inst_id": last_inst_id,
                "synced_at": datetime.now(timezone.utc),
                "author_id": author_id
            })

    preserve_existing_orcid = False
    preserve_existing_openalex_id = False
    while True:
        try:
            execute_update(
                preserve_existing_orcid=preserve_existing_orcid,
                preserve_existing_openalex_id=preserve_existing_openalex_id,
            )
            break
        except IntegrityError as exc:
            # OpenAlex can return identifiers already owned by another local
            # Author row (for example after OpenAlex merges duplicate authors).
            # Preserve only the conflicting local identifier and still apply the
            # remaining details so one author cannot abort its 50-author chunk.
            error_text = str(exc)
            if "Author_orcid_key" in error_text and not preserve_existing_orcid:
                preserve_existing_orcid = True
                print(
                    f"  [WARN] author_id={author_id}: OpenAlex ORCID already belongs to "
                    "another local Author; preserving the local ORCID.",
                    flush=True,
                )
                continue
            if (
                "Author_openalex_id_key" in error_text
                and not preserve_existing_openalex_id
            ):
                preserve_existing_openalex_id = True
                print(
                    f"  [WARN] author_id={author_id}: OpenAlex ID already belongs to "
                    "another local Author; preserving the local OpenAlex ID.",
                    flush=True,
                )
                continue
            raise


def _sync_authors_chunk(engine, chunk, is_orcid, id_to_author, orcid_to_author, author_map):
    local_synced = 0
    
    if is_orcid:
        formatted_orcids = []
        for orcid in chunk:
            if not orcid.startswith("https://orcid.org/"):
                formatted_orcids.append(f"https://orcid.org/{orcid}")
            else:
                formatted_orcids.append(orcid)
        filter_str = "|".join(formatted_orcids)
        url = f"https://api.openalex.org/authors?filter=orcid:{filter_str}&per_page=100"
    else:
        filter_str = "|".join(chunk)
        url = f"https://api.openalex.org/authors?filter=openalex:{filter_str}&per_page=100"

    # Tránh spam API bằng cách sleep nhẹ giữa các luồng
    time.sleep(0.1)
    response = safe_get(url, timeout=25)
    
    if response.status_code == 200:
        results = response.json().get("results", [])
        for data in results:
            author_id = None
            if is_orcid:
                orcid = data.get("orcid")
                clean_orcid = orcid.replace("https://orcid.org/", "").strip() if orcid else None
                if clean_orcid in orcid_to_author:
                    author_id = orcid_to_author[clean_orcid]
                elif orcid in orcid_to_author:
                    author_id = orcid_to_author[orcid]
            else:
                oa_id = data.get("id")
                clean_oa_id = oa_id.split("/")[-1] if oa_id else None
                if clean_oa_id in id_to_author:
                    author_id = id_to_author[clean_oa_id]
                elif oa_id in id_to_author:
                    author_id = id_to_author[oa_id]
                    
            if author_id:
                author_map[author_id]["found"] = True
                update_author_in_db(engine, author_id, data)
                local_synced += 1
    elif response.status_code == 429:
        print("[CRITICAL] OpenAlex API has blocked your IP (HTTP 429). Stop requested.")
        return -1
    else:
        print(f"  [ERROR] Failed to query chunk (HTTP {response.status_code})")
        
    return local_synced


def sync_authors(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Truy vấn các author chưa đồng bộ chi tiết OpenAlex, ưu tiên những người có nhiều bài viết trong DB cục bộ trước
    query = """
        SELECT a.author_id, a.orcid, a.openalex_id, a.display_name, COUNT(aa.article_id) as local_works_count
        FROM "Author" a
        LEFT JOIN "Author_Article" aa ON a.author_id = aa.author_id
        WHERE (a.created_at IS NULL OR a.h_index IS NULL)
          AND (a.orcid IS NOT NULL OR a.openalex_id IS NOT NULL)
        GROUP BY a.author_id, a.orcid, a.openalex_id, a.display_name
        ORDER BY local_works_count DESC, a.author_id ASC
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        authors = conn.execute(text(query)).fetchall()

    if not authors:
        print("[INFO] No authors need synchronization.")
        return

    print(f"[sync-authors] Starting bulk synchronization for {len(authors)} authors...")
    
    # Bản đồ mapping nhanh ID -> author_id và ORCID -> author_id
    id_to_author = {}
    orcid_to_author = {}
    
    author_ids_to_query = []
    orcids_to_query = []
    
    author_map = {}
    
    for author in authors:
        author_id = author[0]
        orcid_raw = author[1]
        openalex_id_raw = author[2]
        display_name = author[3] or "Unknown"
        
        author_map[author_id] = {
            "id": author_id,
            "display_name": display_name,
            "orcid": orcid_raw,
            "openalex_id": openalex_id_raw,
            "found": False
        }
        
        if openalex_id_raw:
            clean_id = openalex_id_raw.split("/")[-1] if "/" in openalex_id_raw else openalex_id_raw
            id_to_author[clean_id] = author_id
            id_to_author[openalex_id_raw] = author_id
            author_ids_to_query.append(clean_id)
        elif orcid_raw:
            clean_orcid = orcid_raw.replace("https://orcid.org/", "").strip()
            orcid_to_author[clean_orcid] = author_id
            orcid_to_author[orcid_raw] = author_id
            orcids_to_query.append(clean_orcid)
    chunk_size = 50
    synced_count = 0
    failed_count = 0
    max_workers = 8  # Số lượng workers để chạy đa luồng đồng thời
    
    # --- LUỒNG 1: Gom theo openalex_id ---
    if author_ids_to_query:
        print(f"[sync-authors] Querying {len(author_ids_to_query)} authors by OpenAlex ID in parallel using {max_workers} threads...")
        chunks = [author_ids_to_query[i:i+chunk_size] for i in range(0, len(author_ids_to_query), chunk_size)]
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _sync_authors_chunk, engine, chunk, False, id_to_author, orcid_to_author, author_map
                ): idx for idx, chunk in enumerate(chunks)
            }
            
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    res = future.result()
                    if res == -1:
                        print("[CRITICAL] Stopping process due to API Rate Limit (429).")
                        sys.exit(1)
                    synced_count += res
                    print(f"  -> Finished ID chunk {idx+1}/{len(chunks)} ({res} authors updated)")
                except Exception as e:
                    print(f"  [ERROR] Thread failed processing ID chunk {idx+1}: {e}")
                    
    # --- LUỒNG 2: Gom theo orcid ---
    if orcids_to_query:
        print(f"[sync-authors] Querying {len(orcids_to_query)} authors by ORCID in parallel using {max_workers} threads...")
        chunks = [orcids_to_query[i:i+chunk_size] for i in range(0, len(orcids_to_query), chunk_size)]
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _sync_authors_chunk, engine, chunk, True, id_to_author, orcid_to_author, author_map
                ): idx for idx, chunk in enumerate(chunks)
            }
            
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    res = future.result()
                    if res == -1:
                        print("[CRITICAL] Stopping process due to API Rate Limit (429).")
                        sys.exit(1)
                    synced_count += res
                    print(f"  -> Finished ORCID chunk {idx+1}/{len(chunks)} ({res} authors updated)")
                except Exception as e:
                    print(f"  [ERROR] Thread failed processing ORCID chunk {idx+1}: {e}")
                    
    # --- LUỒNG 3: Đánh dấu các tác giả không tìm thấy ---
    not_found_authors = [v for v in author_map.values() if not v["found"]]
    if not_found_authors:
        print(f"[sync-authors] Marking {len(not_found_authors)} authors not found in OpenAlex as scanned...")
        # Để an toàn ghi nhận DB tuần tự trong thread chính
        with engine.begin() as conn:
            for item in not_found_authors:
                conn.execute(text("""
                    UPDATE "Author"
                    SET created_at = :synced_at
                    WHERE author_id = :author_id
                """), {
                    "synced_at": datetime.now(timezone.utc),
                    "author_id": item["id"]
                })
                failed_count += 1
                
    print(f"\n[sync-authors] Finished! Synced: {synced_count}, Not Found/Skipped: {failed_count}")


def cmd_stats_authors():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text('SELECT COUNT(*) FROM "Author"')).scalar()
        synced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE created_at IS NOT NULL')).scalar()
        unsynced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE created_at IS NULL AND (orcid IS NOT NULL OR openalex_id IS NOT NULL)')).scalar()
        
        print("\n[OpenAlex Author Sync Stats]")
        print(f"  Total authors in DB:       {total:,}")
        pct = (synced / total * 100) if total > 0 else 0.0
        print(f"  Synced details from OpenAlex: {synced:,} ({pct:.1f}%)")
        print(f"  Pending sync details:      {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Authors Sample]")
            rows = conn.execute(text("""
                SELECT display_name, orcid, openalex_id, works_count, cited_by_count, h_index, last_known_institution
                FROM "Author"
                WHERE created_at IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 10
            """)).fetchall()
            
            print(f"  {'Author Name':<25} {'ORCID':<20} {'Works':>6} {'Citations':>10} {'H-index':>8} {'Institution':<25}")
            print("  " + "-" * 98)
            for r in rows:
                name = r[0][:22] + "..." if r[0] and len(r[0]) > 25 else (r[0] or "N/A")
                orcid = r[1].replace("https://orcid.org/", "") if r[1] else "N/A"
                works = f"{r[3]:,}" if r[3] is not None else "N/A"
                cites = f"{r[4]:,}" if r[4] is not None else "N/A"
                h_idx = f"{r[5]}" if r[5] is not None else "N/A"
                inst = r[6][:22] + "..." if r[6] and len(r[6]) > 25 else (r[6] or "N/A")
                print(f"  {name:<25} {orcid:<20} {works:>6} {cites:>10} {h_idx:>8} {inst:<25}")


def cmd_export_authors(args):
    import pandas as pd
    engine = create_engine(DATABASE_URL)
    
    print("[export-authors] Fetching enriched author data from PostgreSQL...")
    query = """
        SELECT 
            author_id,
            display_name,
            orcid,
            openalex_id,
            works_count,
            cited_by_count,
            h_index,
            i10_index,
            last_known_institution,
            last_known_institution_id,
            created_at
        FROM "Author"
        ORDER BY cited_by_count DESC NULLS LAST, h_index DESC NULLS LAST
    """
    
    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)
            
        if df.empty:
            print("[export-authors] No author data found in database to export.")
            return
            
        print(f"\n[export-authors] Enriched Authors Preview (Top {args.limit}):")
        preview_cols = ["display_name", "orcid", "works_count", "cited_by_count", "h_index", "last_known_institution"]
        print(df[preview_cols].head(args.limit).to_string(index=False))
        
        # Save to CSV
        output_file = args.output
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n[OK] Exported {len(df)} enriched authors to CSV: {output_file}")
        
        # Save to Excel (.xlsx)
        excel_file = os.path.splitext(output_file)[0] + ".xlsx"
        try:
            df.to_excel(excel_file, index=False)
            print(f"[OK] Exported {len(df)} enriched authors to Excel: {excel_file}")
        except Exception as e:
            print(f"[WARNING] Could not save Excel file: {e}")
            
    except Exception as e:
        print(f"[ERROR] Failed to export author data: {e}")


def _db_process_single_work(
    conn, work, journal_uuid,
    topic_cache, author_id_cache, author_orcid_cache, author_name_cache,
    keyword_cache, volume_cache, issue_cache, cache_lock
):
    work_title = work.get("title")
    if not work_title:
        return
        
    # Rút ngắn tiêu đề nếu quá dài để tránh lỗi db varchar
    work_title = work_title[:255]
    doi = work.get("doi")
    pub_year = work.get("publication_year")
    
    # Trích xuất Abstract từ abstract_inverted_index
    abstract = None
    inv_index = work.get("abstract_inverted_index")
    if inv_index:
        try:
            word_list = []
            for word, pos_list in inv_index.items():
                for pos in pos_list:
                    word_list.append((pos, word))
            word_list.sort()
            abstract = " ".join([w[1] for w in word_list])
        except Exception:
            pass
    if abstract:
        abstract = abstract[:2000]
        
    # 1.5 Xử lý Volume và Issue
    import re
    biblio = work.get("biblio", {})
    volume_raw = biblio.get("volume")
    issue_raw = biblio.get("issue")
    
    volume_number = None
    if volume_raw is not None:
        match = re.search(r'\d+', str(volume_raw))
        if match:
            try:
                volume_number = int(match.group())
            except ValueError:
                pass
                
    volume_uuid = None
    if volume_number is not None:
        vol_key = (volume_number, pub_year)
        if vol_key in volume_cache:
            volume_uuid = volume_cache[vol_key]
        else:
            v_row = conn.execute(text("""
                SELECT volume_id FROM "Volume"
                WHERE journal_id = :journal_id AND volume_number = :volume_number AND publication_year = :year
            """), {
                "journal_id": journal_uuid,
                "volume_number": volume_number,
                "year": pub_year
            }).fetchone()
            
            if v_row:
                volume_uuid = v_row[0]
                # Khôi phục nếu bị xóa logic
                conn.execute(text("""
                    UPDATE "Volume" SET is_deleted = false WHERE volume_id = :volume_id
                """), {"volume_id": volume_uuid})
            else:
                volume_uuid = conn.execute(text("""
                    INSERT INTO "Volume" (journal_id, volume_number, publication_year)
                    VALUES (:journal_id, :volume_number, :year)
                    RETURNING volume_id
                """), {
                    "journal_id": journal_uuid,
                    "volume_number": volume_number,
                    "year": pub_year
                }).scalar()
            volume_cache[vol_key] = volume_uuid
                
    issue_uuid = None
    if volume_uuid is not None and issue_raw is not None:
        issue_str = str(issue_raw)[:50]
        iss_key = (volume_uuid, issue_str, pub_year)
        if iss_key in issue_cache:
            issue_uuid = issue_cache[iss_key]
        else:
            i_row = conn.execute(text("""
                SELECT issue_id FROM "Issue"
                WHERE volume_id = :volume_id AND issue_number = :issue_number AND publication_year = :year
            """), {
                "volume_id": volume_uuid,
                "issue_number": issue_str,
                "year": pub_year
            }).fetchone()
            
            if i_row:
                issue_uuid = i_row[0]
            else:
                issue_uuid = conn.execute(text("""
                    INSERT INTO "Issue" (volume_id, issue_number, publication_year)
                    VALUES (:volume_id, :issue_number, :year)
                    RETURNING issue_id
                """), {
                    "volume_id": volume_uuid,
                    "issue_number": issue_str,
                    "year": pub_year
                }).scalar()
            issue_cache[iss_key] = issue_uuid
            
    # 2. Xử lý primary_topic
    primary_topic = work.get("primary_topic")
    primary_topic_uuid = None
    if primary_topic:
        t_name = primary_topic.get("display_name")
        t_score = primary_topic.get("score")
        if t_score is not None:
            try:
                t_score = float(t_score)
            except ValueError:
                t_score = 0.0
        else:
            t_score = 0.0
            
        field_data = primary_topic.get("field") or {}
        field_name = field_data.get("display_name")
        
        subfield_data = primary_topic.get("subfield") or {}
        subfield_name = subfield_data.get("display_name")
        
        if t_name:
            with cache_lock:
                if t_name in topic_cache:
                    primary_topic_uuid = topic_cache[t_name]
                else:
                    primary_topic_uuid = get_or_create_topic(
                        conn, t_name, t_score, field_name, subfield_name
                    )
                    topic_cache[t_name] = primary_topic_uuid
                    
    # 3. Chèn hoặc cập nhật thông tin bài báo (Article)
    citation_count = work.get("cited_by_count")
    references_json = json.dumps(work.get("referenced_works", []), ensure_ascii=False)
    reference_count = len(work.get("referenced_works", []) or [])

    article_uuid = None
    art_row = conn.execute(text("""
        SELECT article_id FROM "Article" WHERE title = :title AND issue_id = :issue_id AND is_deleted = false
    """), {
        "title": work_title,
        "issue_id": issue_uuid
    }).fetchone()
    
    if art_row:
        article_uuid = art_row[0]
        conn.execute(text("""
            UPDATE "Article"
            SET doi = :doi,
                abstract = :abstract,
                publication_year = :pub_year,
                primary_topic = :primary_topic,
                citation_count = :citation_count,
                "references" = CAST(:references_json AS JSONB),
                reference_count = :reference_count,
                is_deleted = false
            WHERE article_id = :article_id
        """), {
            "article_id": article_uuid,
            "doi": doi,
            "abstract": abstract,
            "pub_year": pub_year,
            "primary_topic": primary_topic_uuid,
            "citation_count": citation_count,
            "references_json": references_json,
            "reference_count": reference_count,
        })
    else:
        article_uuid = conn.execute(text("""
            INSERT INTO "Article" (
                title, doi, abstract, issue_id, publication_year, primary_topic,
                citation_count, "references", reference_count
            )
            VALUES (
                :title, :doi, :abstract, :issue_id, :pub_year, :primary_topic,
                :citation_count, CAST(:references_json AS JSONB), :reference_count
            )
            RETURNING article_id
        """), {
            "title": work_title,
            "doi": doi,
            "abstract": abstract,
            "issue_id": issue_uuid,
            "pub_year": pub_year,
            "primary_topic": primary_topic_uuid,
            "citation_count": citation_count,
            "references_json": references_json,
            "reference_count": reference_count,
        }).scalar()
            
    # 4. Xử lý Authorship (Tác giả và liên kết tác giả với bài viết)
    authorships = work.get("authorships", [])
    for auth_item in authorships:
        author_data = auth_item.get("author", {})
        auth_name = author_data.get("display_name")
        auth_orcid = author_data.get("orcid")
        auth_openalex_id = author_data.get("id")
        
        if auth_name:
            author_uuid = None

            # Get-or-create phai serialize qua cac thread de tranh tao trung
            # Author khi 2 luong cung gap 1 tac gia moi lan dau (race condition).
            with cache_lock:
                # 4.1 Tra cứu cache trước
                if auth_openalex_id and auth_openalex_id in author_id_cache:
                    author_uuid = author_id_cache[auth_openalex_id]
                elif auth_orcid and auth_orcid in author_orcid_cache:
                    author_uuid = author_orcid_cache[auth_orcid]
                elif not auth_openalex_id and not auth_orcid and auth_name in author_name_cache:
                    author_uuid = author_name_cache[auth_name]

                # 4.2 Nếu không có trong cache, truy vấn DB
                if not author_uuid:
                    # Ưu tiên tìm theo OpenAlex ID
                    if auth_openalex_id:
                        a_row = conn.execute(text("""
                            SELECT author_id FROM "Author" WHERE openalex_id = :oid
                        """), {"oid": auth_openalex_id}).fetchone()
                        if a_row:
                            author_uuid = a_row[0]

                    # Tìm theo ORCID nếu không tìm thấy theo OpenAlex ID
                    if not author_uuid and auth_orcid:
                        a_row = conn.execute(text("""
                            SELECT author_id FROM "Author" WHERE orcid = :orcid
                        """), {"orcid": auth_orcid}).fetchone()
                        if a_row:
                            author_uuid = a_row[0]

                    # Tìm theo Tên nếu không có ID nào
                    if not author_uuid:
                        a_row = conn.execute(text("""
                            SELECT author_id FROM "Author" WHERE display_name = :name AND openalex_id IS NULL AND orcid IS NULL
                        """), {"name": auth_name}).fetchone()
                        if a_row:
                            author_uuid = a_row[0]

                # 4.3 Nếu vẫn không tìm thấy, tạo tác giả mới
                if not author_uuid:
                    author_uuid = conn.execute(text("""
                        INSERT INTO "Author" (display_name, orcid, openalex_id)
                        VALUES (:name, :orcid, :openalex_id)
                        RETURNING author_id
                    """), {
                        "name": auth_name,
                        "orcid": auth_orcid,
                        "openalex_id": auth_openalex_id
                    }).scalar()

                # 4.4 Cập nhật cache
                if auth_openalex_id:
                    author_id_cache[auth_openalex_id] = author_uuid
                if auth_orcid:
                    author_orcid_cache[auth_orcid] = author_uuid
                if not auth_openalex_id and not auth_orcid:
                    author_name_cache[auth_name] = author_uuid

            # Liên kết Author và Article (khong can lock: chi phu thuoc author_uuid da resolve)
            if author_uuid and article_uuid:
                conn.execute(text("""
                    INSERT INTO "Author_Article" (author_id, article_id)
                    VALUES (:author_id, :article_id)
                    ON CONFLICT DO NOTHING
                """), {
                    "author_id": author_uuid,
                    "article_id": article_uuid
                })
                
    # 5. Xử lý Keywords
    keywords = work.get("keywords", [])
    for kw in keywords:
        kw_name = kw.get("display_name")
        kw_score = kw.get("score", 0.0)
        if kw_name:
            if kw_name in keyword_cache:
                kw_uuid = keyword_cache[kw_name]
            else:
                kw_uuid = get_or_create_keyword(conn, kw_name)
                keyword_cache[kw_name] = kw_uuid

            conn.execute(text("""
                INSERT INTO "Keyword_Article" (keyword_id, article_id, score)
                VALUES (:keyword_id, :article_id, :score)
                ON CONFLICT DO NOTHING
            """), {
                "keyword_id": kw_uuid,
                "article_id": article_uuid,
                "score": kw_score
            })
            
    # 6. Xử lý Sub_Topic (các chủ đề phụ/chủ đề khác ngoài primary_topic)
    topics = work.get("topics", [])
    for t_item in topics:
        t_name = t_item.get("display_name")
        t_score = t_item.get("score", 0.0)
        if not t_name:
            continue
            
        field_data = t_item.get("field") or {}
        field_name = field_data.get("display_name")
        
        subfield_data = t_item.get("subfield") or {}
        subfield_name = subfield_data.get("display_name")
        
        with cache_lock:
            if t_name in topic_cache:
                sub_topic_uuid = topic_cache[t_name]
            else:
                sub_topic_uuid = get_or_create_topic(
                    conn, t_name, t_score, field_name, subfield_name
                )
                topic_cache[t_name] = sub_topic_uuid

        conn.execute(text("""
            INSERT INTO "Sub_Topic" (article_id, topic_id)
            VALUES (:article_id, :topic_id)
            ON CONFLICT DO NOTHING
        """), {
            "article_id": article_uuid,
            "topic_id": sub_topic_uuid
        })


def _db_bulk_insert_chunk(raw_conn, chunk_works, topic_cache, kw_cache, author_id_cache, author_orcid_cache, vol_cache, iss_cache):
    """
    Bulk insert an entire chunk (e.g. 10 journals / ~1,000 works) in a single fast transaction.
    Uses execute_values for Authors, Articles, Author_Article, Keyword_Article, Sub_Topic.
    """
    t0 = time.time()
    cur = raw_conn.cursor()

    # 1. Bulk insert any missing keywords
    missing_kws = set()
    for _, _, works in chunk_works:
        for w in works:
            for kw in w.get("keywords", []):
                kn = kw.get("display_name")
                if kn and kn not in kw_cache:
                    missing_kws.add(kn[:255])
    if missing_kws:
        kw_tuples = [(kn,) for kn in missing_kws]
        res = execute_values(cur, """
            INSERT INTO "Keyword" (display_name)
            VALUES %s
            ON CONFLICT (display_name) DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING display_name, keyword_id
        """, kw_tuples, fetch=True)
        for kn, kid in res:
            kw_cache[kn] = kid

    # 2. Volumes, Issues, and Article Tuples
    articles_dict = {}    # openalex_id -> article tuple
    work_refs = []        # list of work dicts
    new_authors_dict = {} # openalex_id -> (display_name, clean_orcid, openalex_id)
    batch_orcids = set(author_orcid_cache.keys())

    for j_id, j_name, works in chunk_works:
        for w in works:
            w_id = w.get("id")
            if not w_id:
                continue
            title = w.get("title")
            if not title:
                continue
            title = title[:1000]
            doi = w.get("doi")
            abstract = w.get("abstract")
            pub_year = w.get("publication_year")
            biblio = w.get("biblio") or {}
            vol_raw = biblio.get("volume")
            iss_raw = biblio.get("issue")

            # Extract volume number
            volume_number = None
            if vol_raw is not None:
                match = re.search(r'\d+', str(vol_raw))
                if match:
                    try:
                        volume_number = int(match.group())
                    except ValueError:
                        pass

            vol_id = None
            if volume_number is not None:
                vol_key = (j_id, volume_number, pub_year)
                if vol_key in vol_cache:
                    vol_id = vol_cache[vol_key]
                else:
                    cur.execute("""
                        SELECT volume_id FROM "Volume"
                        WHERE journal_id = %s AND volume_number = %s AND publication_year = %s AND is_deleted = false
                    """, (j_id, volume_number, pub_year))
                    res = cur.fetchone()
                    if res:
                        vol_id = res[0]
                    else:
                        cur.execute("""
                            INSERT INTO "Volume" (journal_id, volume_number, publication_year)
                            VALUES (%s, %s, %s)
                            RETURNING volume_id
                        """, (j_id, volume_number, pub_year))
                        vol_id = cur.fetchone()[0]
                    vol_cache[vol_key] = vol_id

            iss_id = None
            if vol_id is not None and iss_raw is not None:
                iss_str = str(iss_raw)[:50]
                iss_key = (vol_id, iss_str, pub_year)
                if iss_key in iss_cache:
                    iss_id = iss_cache[iss_key]
                else:
                    cur.execute("""
                        SELECT issue_id FROM "Issue"
                        WHERE volume_id = %s AND issue_number = %s AND publication_year = %s
                    """, (vol_id, iss_str, pub_year))
                    res = cur.fetchone()
                    if res:
                        iss_id = res[0]
                    else:
                        cur.execute("""
                            INSERT INTO "Issue" (volume_id, issue_number, publication_year)
                            VALUES (%s, %s, %s)
                            RETURNING issue_id
                        """, (vol_id, iss_str, pub_year))
                        iss_id = cur.fetchone()[0]
                    iss_cache[iss_key] = iss_id

            # Primary topic
            p_top = w.get("primary_topic") or {}
            t_name = p_top.get("display_name")
            p_top_id = topic_cache.get(t_name) if t_name else None

            cit_cnt = w.get("cited_by_count", 0)
            ref_works = w.get("referenced_works", []) or []
            refs_json = json.dumps(ref_works, ensure_ascii=False)
            ref_cnt = len(ref_works)

            if w_id not in articles_dict:
                articles_dict[w_id] = (
                    title, doi, abstract, iss_id, pub_year, p_top_id,
                    cit_cnt, refs_json, ref_cnt, w_id
                )
                work_refs.append(w)

            # Authors
            for auth_item in w.get("authorships", []):
                a_data = auth_item.get("author") or {}
                a_id = a_data.get("id")
                a_name = a_data.get("display_name")
                a_orcid = a_data.get("orcid")
                if not a_name or not a_id:
                    continue

                if a_id in author_id_cache:
                    continue
                if a_orcid and a_orcid in author_orcid_cache:
                    author_id_cache[a_id] = author_orcid_cache[a_orcid]
                    continue

                if a_id not in new_authors_dict:
                    clean_orcid = a_orcid
                    if clean_orcid:
                        if clean_orcid in batch_orcids:
                            clean_orcid = None
                        else:
                            batch_orcids.add(clean_orcid)
                    new_authors_dict[a_id] = (a_name[:255], clean_orcid, a_id)

    # 3. Bulk insert Authors
    if new_authors_dict:
        auth_tuples = list(new_authors_dict.values())
        try:
            cur_res = execute_values(cur, """
                INSERT INTO "Author" (display_name, orcid, openalex_id)
                VALUES %s
                ON CONFLICT (openalex_id) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING author_id, openalex_id, orcid
            """, auth_tuples, fetch=True)
            for r in cur_res:
                author_id_cache[r[1]] = r[0]
                if r[2]:
                    author_orcid_cache[r[2]] = r[0]
        except Exception:
            raw_conn.rollback()
            # Retry individually to safely handle any unique conflict
            for a_name, a_orcid, a_id in auth_tuples:
                try:
                    cur.execute("""
                        INSERT INTO "Author" (display_name, orcid, openalex_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (openalex_id) DO UPDATE SET display_name = EXCLUDED.display_name
                        RETURNING author_id, openalex_id, orcid
                    """, (a_name, a_orcid, a_id))
                    r = cur.fetchone()
                    if r:
                        author_id_cache[r[1]] = r[0]
                        if r[2]:
                            author_orcid_cache[r[2]] = r[0]
                except Exception:
                    try:
                        cur.execute("""
                            INSERT INTO "Author" (display_name, orcid, openalex_id)
                            VALUES (%s, NULL, %s)
                            ON CONFLICT (openalex_id) DO UPDATE SET display_name = EXCLUDED.display_name
                            RETURNING author_id, openalex_id
                        """, (a_name, a_id))
                        r = cur.fetchone()
                        if r:
                            author_id_cache[r[1]] = r[0]
                    except Exception:
                        pass

    # 4. Bulk insert Articles
    art_map = {}
    articles_data = list(articles_dict.values())
    if articles_data:
        res_arts = execute_values(cur, """
            INSERT INTO "Article" (
                title, doi, abstract, issue_id, publication_year, primary_topic,
                citation_count, "references", reference_count, openalex_id
            )
            VALUES %s
            ON CONFLICT (openalex_id) DO UPDATE 
            SET doi = EXCLUDED.doi,
                abstract = EXCLUDED.abstract,
                publication_year = EXCLUDED.publication_year,
                primary_topic = EXCLUDED.primary_topic,
                citation_count = EXCLUDED.citation_count,
                "references" = EXCLUDED."references",
                reference_count = EXCLUDED.reference_count,
                is_deleted = false
            RETURNING article_id, openalex_id
        """, articles_data, fetch=True)
        for r in res_arts:
            art_map[r[1]] = r[0]

    # 5. Bulk insert join tables
    author_article_set = set()
    keyword_article_set = set()
    sub_topic_set = set()

    for w in work_refs:
        w_id = w.get("id")
        art_id = art_map.get(w_id)
        if not art_id:
            continue

        for auth_item in w.get("authorships", []):
            a_data = auth_item.get("author") or {}
            a_id = a_data.get("id")
            if a_id and a_id in author_id_cache:
                author_article_set.add((author_id_cache[a_id], art_id))

        for kw in w.get("keywords", []):
            k_name = kw.get("display_name")
            k_score = kw.get("score", 0.0)
            if k_name and k_name in kw_cache:
                keyword_article_set.add((kw_cache[k_name], art_id, k_score))

        for top in w.get("topics", []):
            top_name = top.get("display_name")
            if top_name and top_name in topic_cache:
                sub_topic_set.add((art_id, topic_cache[top_name]))

    if author_article_set:
        execute_values(cur, """
            INSERT INTO "Author_Article" (author_id, article_id)
            VALUES %s ON CONFLICT DO NOTHING
        """, list(author_article_set))

    if keyword_article_set:
        execute_values(cur, """
            INSERT INTO "Keyword_Article" (keyword_id, article_id, score)
            VALUES %s ON CONFLICT DO NOTHING
        """, list(keyword_article_set))

    if sub_topic_set:
        execute_values(cur, """
            INSERT INTO "Sub_Topic" (article_id, topic_id)
            VALUES %s ON CONFLICT DO NOTHING
        """, list(sub_topic_set))

    raw_conn.commit()
    elapsed = time.time() - t0
    return len(art_map), elapsed


def _fetch_journal_works(idx, journal, limit):
    journal_uuid, openalex_id, journal_name = journal[0], journal[1], journal[2]
    clean_id = openalex_id.split("/")[-1]

    page_size = min(limit or 100, 100)
    cursor_mode = (limit is None or limit == 0)

    if cursor_mode:
        url = f"https://api.openalex.org/works?filter=primary_location.source.id:{clean_id}&per_page=100&cursor=*"
    else:
        url = f"https://api.openalex.org/works?filter=primary_location.source.id:{clean_id}&per_page={page_size}"

    works_collected = []
    current_url = url
    page_idx = 1
    has_429 = False
    success = True

    while True:
        time.sleep(0.05)
        response = safe_get(current_url, timeout=15)
        if response.status_code != 200:
            success = False
            if response.status_code == 429:
                has_429 = True
            break

        data = response.json()
        results = data.get("results", [])
        meta = data.get("meta", {})

        if not results:
            break

        works_collected.extend(results)

        if not cursor_mode:
            break

        next_cursor = meta.get("next_cursor")
        if not next_cursor or len(works_collected) >= 500:
            break

        import urllib.parse as urllib_parse
        parsed = urllib_parse.urlparse(current_url)
        query_params = urllib_parse.parse_qs(parsed.query)
        query_params['cursor'] = [next_cursor]
        new_query = urllib_parse.urlencode(query_params, doseq=True)
        current_url = parsed._replace(query=new_query).geturl()
        page_idx += 1

    return idx, journal_uuid, journal_name, works_collected, success, has_429


def sync_works(
    limit: int,
    journal_offset: int = 0,
    target_total: int = None,
    prioritize_empty: bool = True,
    batch_journals: int = 10,
):
    engine = create_engine(
        DATABASE_URL, pool_size=5,
        connect_args={"options": "-c statement_timeout=120000 -c synchronous_commit=off"},
    )

    with engine.connect() as conn:
        current_article_count = conn.execute(text('SELECT count(*) FROM "Article" WHERE is_deleted = false')).scalar()

    print(f"\n[sync-works] Current articles in database: {current_article_count:,}")
    if target_total:
        print(f"[sync-works] Target total articles: {target_total:,} (Remaining needed: {max(0, target_total - current_article_count):,})")
        if current_article_count >= target_total:
            print(f"[INFO] Target of {target_total:,} articles is already reached or exceeded! Nothing to do.")
            return

    # 1. Pre-load Global Caches vao RAM de triet tieu SQL SELECT thua
    print("[cache] Pre-loading global topic, keyword, author & orcid caches...")
    t_c0 = time.time()
    with engine.connect() as conn:
        global_topic_cache = {
            r[0]: r[1]
            for r in conn.execute(text('SELECT display_name, topic_id FROM "Topic"')).fetchall()
        }
        global_keyword_cache = {
            r[0]: r[1]
            for r in conn.execute(text('SELECT display_name, keyword_id FROM "Keyword"')).fetchall()
        }
        global_author_id_cache = {
            r[0]: r[1]
            for r in conn.execute(text('SELECT openalex_id, author_id FROM "Author" WHERE openalex_id IS NOT NULL')).fetchall()
        }
        global_author_orcid_cache = {
            r[0]: r[1]
            for r in conn.execute(text('SELECT orcid, author_id FROM "Author" WHERE orcid IS NOT NULL')).fetchall()
        }
    print(
        f"[cache] Pre-loaded {len(global_topic_cache):,} topics, {len(global_keyword_cache):,} keywords, "
        f"{len(global_author_id_cache):,} authors, {len(global_author_orcid_cache):,} orcids in {time.time() - t_c0:.2f}s."
    )

    global_vol_cache = {}
    global_iss_cache = {}

    # 2. Lay danh sach cac Journal da match OpenAlex
    if prioritize_empty:
        print("[sync-works] Ordering journals to prioritize journals with 0 or fewest articles first...")
        query_journals = """
            SELECT j.journal_id, j.source_id, j.display_name, COUNT(a.article_id) as art_count
            FROM "Journal" j
            LEFT JOIN "Volume" v ON j.journal_id = v.journal_id
            LEFT JOIN "Issue" i ON v.volume_id = i.volume_id
            LEFT JOIN "Article" a ON i.issue_id = a.issue_id AND a.is_deleted = false
            WHERE j.source_id LIKE 'https://openalex.org/%%' AND j.is_deleted = false
            GROUP BY j.journal_id, j.source_id, j.display_name
            ORDER BY art_count ASC, j.journal_id ASC
        """
    else:
        query_journals = """
            SELECT journal_id, source_id, display_name
            FROM "Journal"
            WHERE source_id LIKE 'https://openalex.org/%' AND is_deleted = false
            ORDER BY journal_id ASC
        """
    with engine.connect() as conn:
        journals = conn.execute(text(query_journals)).fetchall()

    if not journals:
        print("[INFO] No OpenAlex-matched journals found in database. Please sync journals first.")
        return

    total_journals = len(journals)
    if journal_offset < 0:
        raise ValueError("journal_offset must be >= 0")
    if journal_offset >= total_journals:
        print(f"[INFO] Journal offset {journal_offset:,} is at or beyond the {total_journals:,} available journals.")
        return
    if journal_offset:
        journals = journals[journal_offset:]
        print(
            f"[sync-works] Resuming from ordered journal offset {journal_offset:,}; "
            f"{len(journals):,} journals remain."
        )

    print(f"\n[sync-works] Starting chunked bulk sync for {len(journals)} journals (Batch: {batch_journals} journals/chunk)...")

    synced_works_count = 0
    consecutive_429 = 0
    total_batches = (len(journals) + batch_journals - 1) // batch_journals
    chunk_idx = 0

    raw_conn = engine.raw_connection()
    try:
        for b_start in range(0, len(journals), batch_journals):
            chunk_idx += 1
            b_end = min(b_start + batch_journals, len(journals))
            batch_j = journals[b_start:b_end]

            # 1. Fetch works for this batch of journals concurrently (max 3 workers)
            chunk_works = []
            has_429_count = 0
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(_fetch_journal_works, journal_offset + b_start + i + 1, j, limit)
                    for i, j in enumerate(batch_j)
                ]
                for fut in as_completed(futures):
                    try:
                        idx, j_uuid, j_name, w_list, succ, h429 = fut.result()
                        if h429:
                            has_429_count += 1
                        if w_list:
                            chunk_works.append((j_uuid, j_name, w_list))
                    except Exception as e:
                        print(f"  [Fetch Exception] {e}")

            if has_429_count >= 3:
                consecutive_429 += 1
                if consecutive_429 >= 3:
                    print("\n[CRITICAL] Consecutive HTTP 429 detected. Backing off 60s...")
                    time.sleep(60)
            else:
                consecutive_429 = 0

            # 2. Bulk insert chunk into DB in a single fast transaction
            if chunk_works:
                n_inserted, db_time = _db_bulk_insert_chunk(
                    raw_conn, chunk_works,
                    global_topic_cache, global_keyword_cache,
                    global_author_id_cache, global_author_orcid_cache,
                    global_vol_cache, global_iss_cache
                )
                synced_works_count += n_inserted
                current_article_count += n_inserted

                pct_str = f"({(current_article_count / target_total) * 100:.1f}%)" if target_total else ""
                print(
                    f"[Chunk {chunk_idx}/{total_batches}] Saved {n_inserted} articles "
                    f"from {len(chunk_works)} journals in {db_time:.2f}s DB ({n_inserted / max(0.01, db_time):.0f} art/s) "
                    f"| Total DB Articles: {current_article_count:,} {pct_str}",
                    flush=True
                )

            # Check target
            if target_total and current_article_count >= target_total:
                print(f"\n[TARGET REACHED] Reached target {target_total:,} articles! Stopping gracefully.")
                break

    finally:
        raw_conn.close()

    with engine.connect() as conn:
        final_article_count = conn.execute(text('SELECT count(*) FROM "Article" WHERE is_deleted = false')).scalar()

    print("\n" + "=" * 60)
    print(f"[sync-works] Finished! Total articles in DB: {final_article_count:,} (Session synced: {synced_works_count:,})")
    print("=" * 60)



def cmd_stats_works():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        articles = conn.execute(text('SELECT COUNT(*) FROM "Article" WHERE is_deleted = false')).scalar()
        topics = conn.execute(text('SELECT COUNT(*) FROM "Topic"')).scalar()
        keywords = conn.execute(text('SELECT COUNT(*) FROM "Keyword"')).scalar()
        publishers = conn.execute(text('SELECT COUNT(*) FROM "Publisher"')).scalar()
        volumes = conn.execute(text('SELECT COUNT(*) FROM "Volume" WHERE is_deleted = false')).scalar()
        issues = conn.execute(text('SELECT COUNT(*) FROM "Issue"')).scalar()
        sub_topics = conn.execute(text('SELECT COUNT(*) FROM "Sub_Topic"')).scalar()
        
        print("\n[OpenAlex Academic Entities Stats]")
        print(f"  Total Articles/Works:   {articles:,}")
        print(f"  Total Volumes:          {volumes:,}")
        print(f"  Total Issues:           {issues:,}")
        print(f"  Total Topics:           {topics:,}")
        print(f"  Total Sub_Topics:       {sub_topics:,}")
        print(f"  Total Keywords:         {keywords:,}")
        print(f"  Total Publishers:       {publishers:,}")


def cmd_export_works(args):
    engine = create_engine(DATABASE_URL)
    print("[export-works] Fetching enriched article data from PostgreSQL...")
    
    query = """
        SELECT 
            a.title AS "Title",
            a.doi AS "DOI",
            a.publication_year AS "Publication Year",
            t.display_name AS "Primary Topic",
            COALESCE(STRING_AGG(DISTINCT au.display_name, ', '), '') AS "Authors",
            COALESCE(STRING_AGG(DISTINCT kw.display_name, ', '), '') AS "Keywords",
            a.abstract AS "Abstract"
        FROM "Article" a
        LEFT JOIN "Topic" t ON a.primary_topic = t.topic_id
        LEFT JOIN "Author_Article" aa ON a.article_id = aa.article_id
        LEFT JOIN "Author" au ON aa.author_id = au.author_id
        LEFT JOIN "Keyword_Article" ka ON a.article_id = ka.article_id
        LEFT JOIN "Keyword" kw ON ka.keyword_id = kw.keyword_id
        WHERE a.is_deleted = false
        GROUP BY a.article_id, t.display_name
        ORDER BY a.publication_year DESC, a.title ASC
    """
    
    try:
        import pandas as pd
        with engine.connect() as conn:
            df = pd.read_sql_query(text(query), conn)
            
        if df.empty:
            print("[export-works] No article data found in database to export.")
            return
            
        print(f"\n[export-works] Enriched Articles Preview (Top {args.limit}):")
        preview_cols = ["Title", "Publication Year", "Primary Topic", "Authors"]
        print(df[preview_cols].head(args.limit).to_string(index=False))
        
        # Save to CSV
        output_file = args.output
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n[OK] Exported {len(df)} enriched articles to CSV: {output_file}")
        
        # Save to Excel (.xlsx)
        excel_file = os.path.splitext(output_file)[0] + ".xlsx"
        try:
            df.to_excel(excel_file, index=False)
            print(f"[OK] Exported {len(df)} enriched articles to Excel: {excel_file}")
        except Exception as e:
            print(f"[WARNING] Could not save Excel file: {e}")
            
    except Exception as e:
        print(f"[ERROR] Failed to export article data: {e}")


def main():
    parser = argparse.ArgumentParser(description="OpenAlex Sync Tool")
    sub = parser.add_subparsers(dest="command")
    
    # sync subcommand (Journal)
    p_sync = sub.add_parser("sync", help="Sync journal data from OpenAlex")
    p_sync.add_argument("--limit", type=int, default=None, help="Limit number of journals to sync")
    
    # stats subcommand (Journal)
    sub.add_parser("stats", help="Show OpenAlex journal synchronization stats")

    # export subcommand (Journal)
    p_export = sub.add_parser("export", help="Export enriched journals to CSV/Excel")
    p_export.add_argument("--output", default="data/enriched_journals.csv", help="Output CSV file path")
    p_export.add_argument("--limit", type=int, default=20, help="Number of preview records on screen")
    
    # sync-authors subcommand
    p_sync_authors = sub.add_parser("sync-authors", help="Sync author data from OpenAlex")
    p_sync_authors.add_argument("--limit", type=int, default=None, help="Limit number of authors to sync")
    
    # stats-authors subcommand
    sub.add_parser("stats-authors", help="Show OpenAlex author synchronization stats")
    
    # export-authors subcommand
    p_exp_authors = sub.add_parser("export-authors", help="Export enriched authors to CSV/Excel")
    p_exp_authors.add_argument("--output", default="data/enriched_authors.csv", help="Output CSV file path")
    p_exp_authors.add_argument("--limit", type=int, default=20, help="Number of preview records on screen")
    
    # sync-works subcommand
    p_sync_works = sub.add_parser("sync-works", help="Sync works/articles from OpenAlex for synced journals")
    p_sync_works.add_argument("--limit", type=int, default=None, help="Limit number of works per journal to sync")
    p_sync_works.add_argument(
        "--journal-offset", type=int, default=0,
        help="Skip this many journals in stable journal_id order when resuming",
    )
    p_sync_works.add_argument(
        "--target-total", type=int, default=None,
        help="Stop when total articles in database reaches this number (e.g. 2000000)",
    )
    p_sync_works.add_argument(
        "--no-prioritize-empty", action="store_true", default=False,
        help="Do not prioritize journals with fewest/0 articles first",
    )
    p_sync_works.add_argument(
        "--batch-journals", type=int, default=10,
        help="Number of journals to buffer and insert as a chunk (default: 10)",
    )
    
    # stats-works subcommand
    sub.add_parser("stats-works", help="Show statistics of synced academic entities (Articles, Topics, Keywords)")
    
    # export-works subcommand
    p_exp_works = sub.add_parser("export-works", help="Export enriched articles/works to CSV/Excel")
    p_exp_works.add_argument("--output", default="data/enriched_articles.csv", help="Output CSV file path")
    p_exp_works.add_argument("--limit", type=int, default=20, help="Number of preview records on screen")
    
    args = parser.parse_args()
    
    # Only the write-heavy commands need the lock; stats/export just read and
    # should stay freely runnable at any time for progress checks.
    if args.command == "sync":
        acquire_lock("openalex_sync-sync")
        sync_journals(args.limit)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "sync-authors":
        acquire_lock("openalex_sync-sync-authors")
        sync_authors(args.limit)
    elif args.command == "stats-authors":
        cmd_stats_authors()
    elif args.command == "export-authors":
        cmd_export_authors(args)
    elif args.command == "sync-works":
        acquire_lock("openalex_sync-sync-works")
        sync_works(
            args.limit,
            args.journal_offset,
            target_total=args.target_total,
            prioritize_empty=not args.no_prioritize_empty,
            batch_journals=getattr(args, "batch_journals", 10),
        )
    elif args.command == "stats-works":
        cmd_stats_works()
    elif args.command == "export-works":
        cmd_export_works(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
