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
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from datetime import datetime, timezone
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
        
    retries = 3
    for attempt in range(retries):
        try:
            # Thử gọi kiểm tra SSL thông thường trước
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                check_insufficient_budget(resp)
                print(f"  [API Limit] HTTP 429 (Too Many Requests). Sleeping 10s before retry {attempt+1}/{retries}...")
                time.sleep(10)
                continue
            return resp
        except Exception as e:
            # Bắt các lỗi kết nối/SSL
            print(f"  [SSL/Connection Warning] Attempt {attempt+1}/{retries} failed: {e}. Retrying with verify=False...")
            try:
                resp = requests.get(url, headers=headers, timeout=timeout, verify=False)
                if resp.status_code == 429:
                    check_insufficient_budget(resp)
                    print(f"  [API Limit] HTTP 429 (Too Many Requests). Sleeping 10s...")
                    time.sleep(10)
                    continue
                return resp
            except Exception as e_inner:
                print(f"  [Error] Fallback verify=False failed: {e_inner}")
                if attempt < retries - 1:
                    time.sleep(3)
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


def get_or_create_subject_info(conn, field_name, subfield_name):
    subject_area_id = None
    subject_category_id = None
    
    if field_name:
        area_row = conn.execute(text("""
            SELECT subject_area_id FROM "Subject_Area" WHERE LOWER(display_name) = LOWER(:name)
        """), {"name": field_name}).fetchone()
        if area_row:
            subject_area_id = area_row[0]
        else:
            subject_area_id = conn.execute(text("""
                INSERT INTO "Subject_Area" (display_name)
                VALUES (:name)
                RETURNING subject_area_id
            """), {"name": field_name}).scalar()
            
    if subfield_name:
        cat_row = conn.execute(text("""
            SELECT subject_category_id FROM "Subject_Category" WHERE LOWER(display_name) = LOWER(:name)
        """), {"name": subfield_name}).fetchone()
        if cat_row:
            subject_category_id = cat_row[0]
        else:
            # Tạo code ngẫu nhiên hoặc để trống nếu DB cho phép NULL
            # Vì code là UNIQUE, nếu ta để NULL thì PostgreSQL cho phép nhiều dòng NULL
            # nhưng nếu chèn rỗng/NULL thì an toàn nhất.
            subject_category_id = conn.execute(text("""
                INSERT INTO "Subject_Category" (subject_area_id, display_name)
                VALUES (:area_id, :name)
                RETURNING subject_category_id
            """), {"area_id": subject_area_id, "name": subfield_name}).scalar()
            
    return subject_area_id, subject_category_id


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
        LEFT JOIN (
            SELECT DISTINCT ON (source_id) source_id, rank_txt
            FROM raw_scimago_journal
            ORDER BY source_id, created_at DESC
        ) r ON j.source_id = r.source_id
        WHERE j.source_id NOT LIKE 'https://openalex.org/%' AND j.source_id NOT LIKE 'S%'
        ORDER BY 
            CASE WHEN r.rank_txt IS NULL OR r.rank_txt = '' THEN 999999 
                 ELSE CAST(r.rank_txt AS integer) 
            END ASC, 
            j.journal_id ASC
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
            homepage_url = source_data.get("homepage_url")
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
        total = conn.execute(text('SELECT COUNT(*) FROM "Journal"')).scalar()
        synced = conn.execute(text("SELECT COUNT(*) FROM \"Journal\" WHERE source_id LIKE 'https://openalex.org/%' OR source_id LIKE 'S%'")).scalar()
        unsynced = total - synced
        
        print("\n[OpenAlex Sync Stats]")
        print(f"  Total journals in DB:    {total:,}")
        pct = (synced / total * 100) if total > 0 else 0.0
        print(f"  Synced with OpenAlex:    {synced:,} ({pct:.1f}%)")
        print(f"  Pending sync:            {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Journals Sample]")
            rows = conn.execute(text("""
                SELECT display_name, source_id, works_synced_at
                FROM "Journal"
                WHERE source_id LIKE 'https://openalex.org/%' OR source_id LIKE 'S%'
                ORDER BY works_synced_at DESC NULLS LAST
                LIMIT 10
            """)).fetchall()
            
            print(f"  {'Journal Name':<50} {'Works Synced At':<25} {'OpenAlex ID':<25}")
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
        # 1. Tải toàn bộ Journal
        with engine.connect() as conn:
            journals = conn.execute(text("""
                SELECT journal_id, source_id, issn, scope_detail, display_name, works_synced_at
                FROM "Journal"
            """)).fetchall()
            
        # 2. Tải toàn bộ raw_scimago_journal (chỉ lấy bản ghi mới nhất cho mỗi source_id)
        with engine.connect() as conn:
            raw_rows = conn.execute(text("""
                SELECT DISTINCT ON (source_id) source_id, issn, raw_json
                FROM raw_scimago_journal
                ORDER BY source_id, created_at DESC
            """)).fetchall()
            
        print(f"[export] Loaded {len(journals)} journals and {len(raw_rows)} raw scimago records.")
        
        # 3. Xây dựng mapping ISSN -> raw_row và source_id thô -> raw_row
        raw_by_issn = {}
        raw_by_source_id = {}
        
        for row in raw_rows:
            raw_by_source_id[row.source_id] = row
            issn_str = row.issn or ""
            # Trích xuất và chuẩn hóa ISSNs
            parts = [x.strip() for x in issn_str.replace(",", " ").split() if x.strip()]
            for part in parts:
                if len(part) >= 8:
                    raw_by_issn[part] = row
                    
        # 4. So khớp từng Journal với raw scimago record
        records = []
        matched_count = 0
        
        for j in journals:
            raw_match = None
            
            # Thử tìm theo source_id thô trước (nếu chưa sync)
            if j.source_id in raw_by_source_id:
                raw_match = raw_by_source_id[j.source_id]
                
            # Thử tìm theo ISSN (quan trọng nhất cho các dòng đã sync)
            if not raw_match and j.issn:
                j_parts = [x.strip() for x in j.issn.replace(",", " ").split() if x.strip()]
                for part in j_parts:
                    if len(part) >= 8 and part in raw_by_issn:
                        raw_match = raw_by_issn[part]
                        break
                        
            raw_dict = {}
            if raw_match and raw_match.raw_json:
                matched_count += 1
                if isinstance(raw_match.raw_json, str):
                    raw_dict = json.loads(raw_match.raw_json)
                elif isinstance(raw_match.raw_json, dict):
                    raw_dict = raw_match.raw_json
                    
            if not raw_dict:
                # Fallback nếu không khớp được bản ghi thô
                raw_dict = {
                    "Sourceid": j.source_id,
                    "Title": j.display_name,
                    "Issn": j.issn,
                }
                
            # Override ISSN bằng ISSN sạch từ DB
            if j.issn:
                issn_key = "Issn"
                for k in raw_dict.keys():
                    if k.lower() == "issn":
                        issn_key = k
                        break
                raw_dict[issn_key] = j.issn
                
            # Gắn thêm các cột OpenAlex làm giàu thông tin
            oa_id = j.source_id if (j.source_id and ('openalex.org' in j.source_id or j.source_id.startswith('S'))) else None
            raw_dict["OpenAlex ID"] = oa_id
            raw_dict["OpenAlex Homepage"] = None
            raw_dict["OpenAlex Works Count"] = None
            raw_dict["OpenAlex Cited By Count"] = None
            raw_dict["Scope Detail"] = j.scope_detail
            
            records.append(raw_dict)
            
        print(f"[export] Successfully matched {matched_count}/{len(journals)} journals with their raw Scimago data.")
            
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
        
    homepage_url = data.get("homepage_url")
    
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE "Author"
            SET openalex_id = :openalex_id,
                orcid = :orcid,
                display_name = COALESCE(:disp_name, display_name),
                works_count = :works_count,
                cited_by_count = :cited_by_count,
                h_index = :h_index,
                i10_index = :i10_index,
                last_known_institution = :last_inst_name,
                last_known_institution_id = :last_inst_id,
                homepage_url = :homepage_url,
                openalex_synced_at = :synced_at
            WHERE author_id = :author_id
        """), {
            "openalex_id": openalex_id,
            "orcid": orcid,
            "disp_name": disp_name,
            "works_count": works_count,
            "cited_by_count": cited_by_count,
            "h_index": h_index,
            "i10_index": i10_index,
            "last_inst_name": last_inst_name,
            "last_inst_id": last_inst_id,
            "homepage_url": homepage_url,
            "synced_at": datetime.now(timezone.utc),
            "author_id": author_id
        })


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
        WHERE (a.openalex_synced_at IS NULL OR a.h_index IS NULL)
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
                    SET openalex_synced_at = :synced_at
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
        synced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE openalex_synced_at IS NOT NULL')).scalar()
        unsynced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE openalex_synced_at IS NULL AND (orcid IS NOT NULL OR openalex_id IS NOT NULL)')).scalar()
        
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
                WHERE openalex_synced_at IS NOT NULL
                ORDER BY openalex_synced_at DESC
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
            homepage_url,
            openalex_synced_at
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
    keyword_cache, volume_cache, issue_cache
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
            if t_name in topic_cache:
                primary_topic_uuid = topic_cache[t_name]
            else:
                subject_area_id, subject_category_id = get_or_create_subject_info(
                    conn, field_name, subfield_name
                )
                
                t_row = conn.execute(text("""
                    SELECT topic_id FROM "Topic" WHERE display_name = :name
                """), {"name": t_name}).fetchone()
                
                if t_row:
                    primary_topic_uuid = t_row[0]
                    conn.execute(text("""
                        UPDATE "Topic"
                        SET subject_area_id = COALESCE(subject_area_id, :area_id),
                            subject_category_id = COALESCE(subject_category_id, :cat_id),
                            score = :score
                        WHERE topic_id = :topic_id
                    """), {
                        "area_id": subject_area_id,
                        "cat_id": subject_category_id,
                        "score": t_score,
                        "topic_id": primary_topic_uuid
                    })
                else:
                    primary_topic_uuid = conn.execute(text("""
                        INSERT INTO "Topic" (display_name, score, subject_area_id, subject_category_id)
                        VALUES (:name, :score, :area_id, :cat_id)
                        RETURNING topic_id
                    """), {
                        "name": t_name, 
                        "score": t_score,
                        "area_id": subject_area_id,
                        "cat_id": subject_category_id
                    }).scalar()
                topic_cache[t_name] = primary_topic_uuid
                    
    # 3. Chèn hoặc cập nhật thông tin bài báo (Article)
    article_uuid = None
    art_row = conn.execute(text("""
        SELECT article_id FROM "Article" WHERE title = :title AND issue_id = :issue_id AND is_deleted = false
    """), {
        "title": work_title,
        "issue_id": issue_uuid
    }).fetchone()
    
    if art_row:
        article_uuid = art_row[0]
    else:
        article_uuid = conn.execute(text("""
            INSERT INTO "Article" (title, doi, abstract, issue_id, publication_year, primary_topic)
            VALUES (:title, :doi, :abstract, :issue_id, :pub_year, :primary_topic)
            RETURNING article_id
        """), {
            "title": work_title,
            "doi": doi,
            "abstract": abstract,
            "issue_id": issue_uuid,
            "pub_year": pub_year,
            "primary_topic": primary_topic_uuid
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
                
            # Liên kết Author và Article
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
                kw_row = conn.execute(text("""
                    SELECT keyword_id FROM "Keyword" WHERE display_name = :name
                """), {"name": kw_name}).fetchone()
                
                if kw_row:
                    kw_uuid = kw_row[0]
                else:
                    kw_uuid = conn.execute(text("""
                        INSERT INTO "Keyword" (display_name)
                        VALUES (:name)
                        RETURNING keyword_id
                    """), {"name": kw_name}).scalar()
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
        
        if t_name in topic_cache:
            sub_topic_uuid = topic_cache[t_name]
        else:
            subject_area_id, subject_category_id = get_or_create_subject_info(
                conn, field_name, subfield_name
            )
            
            sub_t_row = conn.execute(text("""
                SELECT topic_id FROM "Topic" WHERE display_name = :name
            """), {"name": t_name}).fetchone()
            
            if sub_t_row:
                sub_topic_uuid = sub_t_row[0]
                conn.execute(text("""
                    UPDATE "Topic"
                    SET subject_area_id = COALESCE(subject_area_id, :area_id),
                        subject_category_id = COALESCE(subject_category_id, :cat_id),
                        score = :score
                    WHERE topic_id = :topic_id
                """), {
                    "area_id": subject_area_id,
                    "cat_id": subject_category_id,
                    "score": t_score,
                    "topic_id": sub_topic_uuid
                })
            else:
                sub_topic_uuid = conn.execute(text("""
                    INSERT INTO "Topic" (display_name, score, subject_area_id, subject_category_id)
                    VALUES (:name, :score, :area_id, :cat_id)
                    RETURNING topic_id
                """), {
                    "name": t_name, 
                    "score": t_score,
                    "area_id": subject_area_id,
                    "cat_id": subject_category_id
                }).scalar()
            topic_cache[t_name] = sub_topic_uuid
            
        conn.execute(text("""
            INSERT INTO "Sub_Topic" (article_id, topic_id)
            VALUES (:article_id, :topic_id)
            ON CONFLICT DO NOTHING
        """), {
            "article_id": article_uuid,
            "topic_id": sub_topic_uuid
        })


def sync_works(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Lấy danh sách các Journal đã được đồng bộ từ OpenAlex nhưng chưa đồng bộ bài viết
    query_journals = """
        SELECT journal_id, source_id, display_name
        FROM "Journal"
        WHERE source_id LIKE 'https://openalex.org/%' AND works_synced_at IS NULL
        ORDER BY journal_id ASC
    """
    with engine.connect() as conn:
        journals = conn.execute(text(query_journals)).fetchall()
        
    if not journals:
        print("[INFO] No synced journals found in database. Please sync journals first.")
        return
        
    print(f"\n[sync-works] Starting synchronization of works/articles for {len(journals)} journals...")
    
    # Cache toàn cục trong suốt lượt chạy sync_works
    topic_cache = {}          # key: display_name -> topic_id
    author_id_cache = {}      # key: openalex_id -> author_id
    author_orcid_cache = {}   # key: orcid -> author_id
    author_name_cache = {}    # key: display_name -> author_id
    keyword_cache = {}        # key: display_name -> keyword_id
    
    synced_works_count = 0
    consecutive_429 = 0
    
    for idx, journal in enumerate(journals, 1):
        journal_uuid = journal[0]
        openalex_id = journal[1]
        journal_name = journal[2]
        
        # Cache cục bộ cho tạp chí hiện tại
        volume_cache = {}  # key: (volume_number, publication_year) -> volume_id
        issue_cache = {}   # key: (volume_id, issue_number, publication_year) -> issue_id
        
        print(f"[{idx}/{len(journals)}] Fetching works for Journal: {journal_name} ({openalex_id})")
        
        # Lấy clean ID của OpenAlex
        clean_id = openalex_id.split("/")[-1]
        
        if limit:
            url = f"https://api.openalex.org/works?filter=primary_location.source.id:{clean_id}&per_page={limit}"
            cursor_mode = False
        else:
            # Lấy full data sử dụng cursor paging, per_page=200 để lấy nhanh nhất có thể
            url = f"https://api.openalex.org/works?filter=primary_location.source.id:{clean_id}&per_page=200&cursor=*"
            cursor_mode = True
            
        try:
            current_url = url
            page_idx = 1
            sync_success = True
            has_429 = False
            
            while True:
                import urllib.parse as urllib_parse
                # Lịch sự tránh spam API
                time.sleep(0.2)
                if cursor_mode:
                    print(f"  -> Fetching page {page_idx} via cursor...")
                else:
                    print(f"  -> Fetching works page...")
                    
                response = safe_get(current_url, timeout=15)
                if response.status_code != 200:
                    print(f"  -> FAILED to fetch works: HTTP {response.status_code}")
                    sync_success = False
                    if response.status_code == 429:
                        has_429 = True
                    break
                    
                data = response.json()
                works = data.get("results", [])
                meta = data.get("meta", {})
                
                if not works:
                    if cursor_mode and page_idx > 1:
                        print("  -> No more works found.")
                    else:
                        print("  -> No works found for this journal.")
                    break
                    
                print(f"  -> Processing {len(works)} works on page {page_idx}...")
                
                with engine.begin() as conn:
                    for work in works:
                        _db_process_single_work(
                            conn, work, journal_uuid,
                            topic_cache, author_id_cache, author_orcid_cache, author_name_cache,
                            keyword_cache, volume_cache, issue_cache
                        )
                        synced_works_count += 1
                
                if not cursor_mode:
                    break
                    
                next_cursor = meta.get("next_cursor")
                if not next_cursor:
                    break
                    
                # Cập nhật URL với next_cursor
                parsed = urllib_parse.urlparse(current_url)
                query_params = urllib_parse.parse_qs(parsed.query)
                query_params['cursor'] = [next_cursor]
                new_query = urllib_parse.urlencode(query_params, doseq=True)
                current_url = parsed._replace(query=new_query).geturl()
                page_idx += 1
                
            if sync_success:
                # Cập nhật thời điểm đồng bộ bài báo thành công để lần sau không quét lại
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE "Journal"
                        SET works_synced_at = :synced_at
                        WHERE journal_id = :journal_id
                    """), {
                        "synced_at": datetime.now(timezone.utc),
                        "journal_id": journal_uuid
                    })
                print(f"  -> SUCCESS: Synced all works for Journal: {journal_name}.")
                consecutive_429 = 0
            else:
                if has_429:
                    print("  -> SKIPPED: Temporary API rate limit (429). Retaining for next run.")
                    consecutive_429 += 1
                    if consecutive_429 >= 3:
                        print("\n[CRITICAL] OpenAlex API has blocked your IP (HTTP 429) consecutively. Stopping process to prevent abuse.")
                        print("Please edit your .env file to set a valid OPENALEX_EMAIL, or wait a few minutes before running again.")
                        sys.exit(1)
                else:
                    consecutive_429 = 0
                    print(f"  -> FAILED: Could not fetch all works for Journal: {journal_name}.")
        except Exception as e:
            print(f"  -> Request Exception for journal {journal_name}: {e}")
            
    print(f"\n[sync-works] Finished! Total synced works/articles: {synced_works_count}")


def cmd_stats_works():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        articles = conn.execute(text('SELECT COUNT(*) FROM "Article" WHERE is_deleted = false')).scalar()
        topics = conn.execute(text('SELECT COUNT(*) FROM "Topic"')).scalar()
        keywords = conn.execute(text('SELECT COUNT(*) FROM "Keyword"')).scalar()
        publishers = conn.execute(text('SELECT COUNT(*) FROM "Publisher"')).scalar()
        volumes = conn.execute(text('SELECT COUNT(*) FROM "Volume"')).scalar()
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
    p_sync_works = sub.add_parser("sync-works", help="Sync works/articles from OpenAlex for synced authors")
    p_sync_works.add_argument("--limit", type=int, default=None, help="Limit number of works per author to sync")
    
    # stats-works subcommand
    sub.add_parser("stats-works", help="Show statistics of synced academic entities (Articles, Topics, Keywords)")
    
    # export-works subcommand
    p_exp_works = sub.add_parser("export-works", help="Export enriched articles/works to CSV/Excel")
    p_exp_works.add_argument("--output", default="data/enriched_articles.csv", help="Output CSV file path")
    p_exp_works.add_argument("--limit", type=int, default=20, help="Number of preview records on screen")
    
    args = parser.parse_args()
    
    if args.command == "sync":
        sync_journals(args.limit)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "sync-authors":
        sync_authors(args.limit)
    elif args.command == "stats-authors":
        cmd_stats_authors()
    elif args.command == "export-authors":
        cmd_export_authors(args)
    elif args.command == "sync-works":
        sync_works(args.limit)
    elif args.command == "stats-works":
        cmd_stats_works()
    elif args.command == "export-works":
        cmd_export_works(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
