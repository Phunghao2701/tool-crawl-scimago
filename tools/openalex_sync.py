"""
OpenAlex Sync Tool
Usage:
  python tools/openalex_sync.py sync --limit 10
  python tools/openalex_sync.py stats
"""

import argparse
import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')
import time
import requests
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


def get_headers():
    # Sử dụng Polite Pool của OpenAlex theo khuyến nghị chính thức
    return {
        "User-Agent": f"ScientificJournalETL/1.0 (mailto:{OPENALEX_EMAIL})"
    }


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
        WHERE j.openalex_synced_at IS NULL
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

    try:
        print(f"[sync] Starting synchronization for {len(journals)} journals...")
        
        synced_count = 0
        failed_count = 0
        
        for idx, journal in enumerate(journals, 1):
            journal_id = journal[0]
            display_name = journal[1]
            issn_str = journal[2] or ""
            source_id = journal[3]
            issns = split_issns(issn_str)
            
            print(f"[{idx}/{len(journals)}] Syncing: {display_name} (ISSNs: {', '.join(issns) if issns else 'None'})")
            
            if not issns:
                print("  -> SKIPPED: No valid ISSNs found for this journal.")
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE "Journal"
                        SET openalex_synced_at = :synced_at
                        WHERE journal_id = :journal_id
                    """), {
                        "synced_at": datetime.now(timezone.utc),
                        "journal_id": journal_id
                    })
                failed_count += 1
                continue

            success = False
            # Gọi thử từng ISSN cho tới khi tìm thấy tạp chí trên OpenAlex
            for issn in issns:
                # Format ISSN để khớp định dạng (ví dụ: xxxx-xxxx)
                formatted_issn = issn
                if len(issn) == 8:
                    formatted_issn = f"{issn[:4]}-{issn[4:]}"
                    
                url = f"https://api.openalex.org/sources?filter=issn:{formatted_issn}"
                try:
                    # Rate limit lịch sự
                    time.sleep(0.2)
                    
                    response = requests.get(url, headers=get_headers(), timeout=15)
                    if response.status_code == 200:
                        data = response.json()
                        results = data.get("results", [])
                        if results:
                            source_data = results[0]
                            openalex_id = source_data.get("id")
                            homepage_url = source_data.get("homepage_url")
                            works_count = source_data.get("works_count")
                            cited_by_count = source_data.get("cited_by_count")
                            
                            # Đồng bộ Publisher nếu có thông tin từ OpenAlex
                            publisher_name = source_data.get("publisher")
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
                            
                            # Cập nhật thông tin vào DB
                            with engine.begin() as conn:
                                conn.execute(text("""
                                    UPDATE "Journal"
                                    SET openalex_id = :openalex_id,
                                        homepage_url = :homepage_url,
                                        works_count = :works_count,
                                        cited_by_count = :cited_by_count,
                                        publisher_id = COALESCE(:publisher_uuid, publisher_id),
                                        openalex_synced_at = :synced_at
                                    WHERE journal_id = :journal_id
                                """), {
                                    "openalex_id": openalex_id,
                                    "homepage_url": homepage_url,
                                    "works_count": works_count,
                                    "cited_by_count": cited_by_count,
                                    "publisher_uuid": publisher_uuid,
                                    "synced_at": datetime.now(timezone.utc),
                                    "journal_id": journal_id
                                })
                            
                            print(f"  -> SUCCESS: OpenAlex ID={openalex_id}, Works={works_count}, Cites={cited_by_count}")
                            success = True
                            break # Đã tìm thấy qua ISSN này, chuyển sang tạp chí khác
                    else:
                        print(f"  -> API Error for ISSN {formatted_issn}: HTTP {response.status_code}")
                except Exception as e:
                    print(f"  -> Request Exception for ISSN {formatted_issn}: {e}")
            
            if success:
                synced_count += 1
            else:
                # Đánh dấu thời điểm sync thất bại / không thấy để tránh bị quét lại liên tục
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE "Journal"
                        SET openalex_synced_at = :synced_at
                        WHERE journal_id = :journal_id
                    """), {
                        "synced_at": datetime.now(timezone.utc),
                        "journal_id": journal_id
                    })
                print("  -> FAILED: Journal not found or API error on all ISSNs.")
                failed_count += 1
    finally:
        pass
        
    print(f"\n[sync] Finished! Synced: {synced_count}, Failed/Not found: {failed_count}")


def cmd_stats(args):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text('SELECT COUNT(*) FROM "Journal"')).scalar()
        synced = conn.execute(text('SELECT COUNT(*) FROM "Journal" WHERE openalex_id IS NOT NULL')).scalar()
        unsynced = conn.execute(text('SELECT COUNT(*) FROM "Journal" WHERE openalex_synced_at IS NULL')).scalar()
        
        print("\n[OpenAlex Sync Stats]")
        print(f"  Total journals in DB:    {total:,}")
        pct = (synced / total * 100) if total > 0 else 0.0
        print(f"  Synced with OpenAlex:    {synced:,} ({pct:.1f}%)")
        print(f"  Pending sync:            {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Journals Sample]")
            rows = conn.execute(text("""
                SELECT display_name, openalex_id, works_count, cited_by_count, homepage_url
                FROM "Journal"
                WHERE openalex_id IS NOT NULL
                ORDER BY openalex_synced_at DESC
                LIMIT 10
            """)).fetchall()
            
            print(f"  {'Journal Name':<50} {'Works':>8} {'Citations':>10} {'OpenAlex ID':<25}")
            print("  " + "-" * 98)
            for r in rows:
                name = r[0][:47] + "..." if len(r[0]) > 50 else r[0]
                works = f"{r[2]:,}" if r[2] is not None else "N/A"
                cites = f"{r[3]:,}" if r[3] is not None else "N/A"
                oid = r[1].replace("https://openalex.org/", "") if r[1] else "N/A"
                print(f"  {name:<50} {works:>8} {cites:>10} {oid:<25}")


def cmd_export(args):
    import pandas as pd
    import json
    engine = create_engine(DATABASE_URL)
    
    print("[export] Fetching enriched data from PostgreSQL...")
    query = """
        SELECT 
            j.source_id,
            j.issn AS normalized_issn,
            j.openalex_id,
            j.homepage_url AS openalex_homepage,
            j.works_count AS openalex_works_count,
            j.cited_by_count AS openalex_cited_by_count,
            j.scope_detail,
            r.raw_json
        FROM "Journal" j
        LEFT JOIN (
            SELECT DISTINCT ON (source_id) source_id, raw_json
            FROM raw_scimago_journal
            ORDER BY source_id, created_at DESC
        ) r ON r.source_id = j.source_id
    """

    
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(query)).fetchall()
            
        records = []
        for row in rows:
            raw_dict = {}
            if row.raw_json:
                if isinstance(row.raw_json, str):
                    raw_dict = json.loads(row.raw_json)
                elif isinstance(row.raw_json, dict):
                    raw_dict = row.raw_json
            
            # Fallback if raw_json is missing
            if not raw_dict:
                raw_dict = {
                    "Sourceid": row.source_id,
                    "Issn": row.normalized_issn,
                }
            
            # Override Issn with normalized ISSN from database (cleaner and verified)
            if row.normalized_issn:
                issn_key = "Issn"
                for k in raw_dict.keys():
                    if k.lower() == "issn":
                        issn_key = k
                        break
                raw_dict[issn_key] = row.normalized_issn
                
            # Dynamically attach OpenAlex fields
            raw_dict["OpenAlex ID"] = row.openalex_id
            raw_dict["OpenAlex Homepage"] = row.openalex_homepage
            raw_dict["OpenAlex Works Count"] = row.openalex_works_count
            raw_dict["OpenAlex Cited By Count"] = row.openalex_cited_by_count
            raw_dict["Scope Detail"] = row.scope_detail
            
            records.append(raw_dict)
            
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


def sync_authors(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Truy vấn các author chưa đồng bộ chi tiết OpenAlex
    query = """
        SELECT author_id, orcid, openalex_id, display_name
        FROM "Author"
        WHERE (openalex_synced_at IS NULL OR h_index IS NULL)
          AND (orcid IS NOT NULL OR openalex_id IS NOT NULL)
        ORDER BY author_id ASC
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        authors = conn.execute(text(query)).fetchall()

    if not authors:
        print("[INFO] No authors need synchronization.")
        return

    try:
        print(f"[sync-authors] Starting synchronization for {len(authors)} authors...")
        
        synced_count = 0
        failed_count = 0
        
        for idx, author in enumerate(authors, 1):
            author_id = author[0]
            orcid_raw = author[1]
            openalex_id_raw = author[2]
            display_name = author[3] or "Unknown"
            
            print(f"[{idx}/{len(authors)}] Syncing Author: {display_name} (ORCID: {orcid_raw}, OpenAlex ID: {openalex_id_raw})")
            
            url = None
            if openalex_id_raw:
                # Lấy phần ID cuối cùng (ví dụ: A5016258957 hoặc url đầy đủ)
                aid = openalex_id_raw.split("/")[-1] if "/" in openalex_id_raw else openalex_id_raw
                url = f"https://api.openalex.org/authors/{aid}"
            elif orcid_raw:
                clean_orcid_val = orcid_raw.split("orcid.org/")[-1] if "orcid.org/" in orcid_raw else orcid_raw
                url = f"https://api.openalex.org/authors/https://orcid.org/{clean_orcid_val}"
                
            if not url:
                print("  -> SKIPPED: No valid ID/ORCID found.")
                failed_count += 1
                continue
                
            try:
                time.sleep(0.2) # Polite pool rate limit
                response = requests.get(url, headers=get_headers(), timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    
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
                    
                    print(f"  -> SUCCESS: Name={disp_name}, Works={works_count}, Cites={cited_by_count}, H-index={h_index}")
                    synced_count += 1
                else:
                    # Đánh dấu thời điểm quét thất bại để tránh lặp vô hạn
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE "Author"
                            SET openalex_synced_at = :synced_at
                            WHERE author_id = :author_id
                        """), {
                            "synced_at": datetime.now(timezone.utc),
                            "author_id": author_id
                        })
                    print(f"  -> FAILED: OpenAlex API returned HTTP {response.status_code}")
                    failed_count += 1
            except Exception as e:
                print(f"  -> Request Exception: {e}")
                failed_count += 1
    finally:
        pass
        
    print(f"\n[sync-authors] Finished! Synced: {synced_count}, Failed/Skipped: {failed_count}")


def cmd_stats_authors():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text('SELECT COUNT(*) FROM "Author"')).scalar()
        synced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE openalex_id IS NOT NULL')).scalar()
        unsynced = conn.execute(text('SELECT COUNT(*) FROM "Author" WHERE openalex_synced_at IS NULL AND (orcid IS NOT NULL OR openalex_id IS NOT NULL)')).scalar()
        
        print("\n[OpenAlex Author Sync Stats]")
        print(f"  Total authors in DB:       {total:,}")
        pct = (synced / total * 100) if total > 0 else 0.0
        print(f"  Synced with OpenAlex:      {synced:,} ({pct:.1f}%)")
        print(f"  Pending sync (with IDs):   {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Authors Sample]")
            rows = conn.execute(text("""
                SELECT display_name, orcid, openalex_id, works_count, cited_by_count, h_index, last_known_institution
                FROM "Author"
                WHERE openalex_id IS NOT NULL
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


def sync_works(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Lấy danh sách các Journal đã được đồng bộ từ OpenAlex
    query_journals = """
        SELECT journal_id, openalex_id, display_name
        FROM "Journal"
        WHERE openalex_id IS NOT NULL
        ORDER BY journal_id ASC
    """
    with engine.connect() as conn:
        journals = conn.execute(text(query_journals)).fetchall()
        
    if not journals:
        print("[INFO] No synced journals found in database. Please sync journals first.")
        return
        
    print(f"\n[sync-works] Starting synchronization of works/articles for {len(journals)} journals...")
    
    synced_works_count = 0
    
    for idx, journal in enumerate(journals, 1):
        journal_uuid = journal[0]
        openalex_id = journal[1]
        journal_name = journal[2]
        
        print(f"[{idx}/{len(journals)}] Fetching works for Journal: {journal_name} ({openalex_id})")
        
        # Lấy clean ID của OpenAlex
        clean_id = openalex_id.split("/")[-1]
        url = f"https://api.openalex.org/works?filter=primary_location.source.id:{clean_id}"
        if limit:
            url += f"&per_page={limit}"
            
        try:
            time.sleep(0.2)  # Rate limit
            response = requests.get(url, headers=get_headers(), timeout=15)
            if response.status_code != 200:
                print(f"  -> FAILED to fetch works: HTTP {response.status_code}")
                continue
                
            data = response.json()
            works = data.get("results", [])
            print(f"  -> Found {len(works)} works.")
            
            for work in works:
                work_title = work.get("title")
                if not work_title:
                    continue
                    
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
                    with engine.begin() as conn:
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
                            
                issue_uuid = None
                if volume_uuid is not None and issue_raw is not None:
                    issue_str = str(issue_raw)[:50]
                    with engine.begin() as conn:
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

                # 2. Xử lý Topic (primary_topic)
                topic_uuid = None
                primary_topic = work.get("primary_topic")
                if primary_topic:
                    topic_name = primary_topic.get("display_name")
                    topic_score = primary_topic.get("score", 0.0)
                    
                    if topic_name:
                        with engine.begin() as conn:
                            t_row = conn.execute(text("""
                                SELECT topic_id FROM "Topic" WHERE display_name = :name
                            """), {"name": topic_name}).fetchone()
                            
                            if t_row:
                                topic_uuid = t_row[0]
                            else:
                                topic_uuid = conn.execute(text("""
                                    INSERT INTO "Topic" (display_name, score)
                                    VALUES (:name, :score)
                                    RETURNING topic_id
                                """), {"name": topic_name, "score": topic_score}).scalar()
                                
                # 3. Tạo hoặc lấy Article
                article_uuid = None
                with engine.begin() as conn:
                    a_row = None
                    if doi:
                        a_row = conn.execute(text("""
                            SELECT article_id FROM "Article" WHERE doi = :doi
                        """), {"doi": doi}).fetchone()
                    if not a_row:
                        a_row = conn.execute(text("""
                            SELECT article_id FROM "Article" WHERE title = :title
                        """), {"title": work_title}).fetchone()
                        
                    if a_row:
                        article_uuid = a_row[0]
                        # Cập nhật issue_id nếu trước đó chưa có
                        conn.execute(text("""
                            UPDATE "Article"
                            SET issue_id = COALESCE(issue_id, :issue_id)
                            WHERE article_id = :article_id
                        """), {"issue_id": issue_uuid, "article_id": article_uuid})
                    else:
                        article_uuid = conn.execute(text("""
                            INSERT INTO "Article" (title, abstract, publication_year, doi, primary_topic, issue_id)
                            VALUES (:title, :abstract, :year, :doi, :topic_id, :issue_id)
                            RETURNING article_id
                        """), {
                            "title": work_title,
                            "abstract": abstract,
                            "year": pub_year,
                            "doi": doi,
                            "topic_id": topic_uuid,
                            "issue_id": issue_uuid
                        }).scalar()
                        
                # 4. Trích xuất tác giả thực tế từ authorships và liên kết với Article
                authorships = work.get("authorships", [])
                for auth_item in authorships:
                    author_data = auth_item.get("author") or {}
                    auth_openalex_id = author_data.get("id")
                    auth_name = author_data.get("display_name")
                    auth_orcid = author_data.get("orcid")
                    
                    if not auth_openalex_id or not auth_name:
                        continue
                        
                    with engine.begin() as conn:
                        author_row = conn.execute(text("""
                            SELECT author_id FROM "Author" WHERE openalex_id = :openalex_id
                        """), {"openalex_id": auth_openalex_id}).fetchone()
                        
                        if author_row:
                            author_uuid = author_row[0]
                            if auth_orcid:
                                conn.execute(text("""
                                    UPDATE "Author"
                                    SET orcid = COALESCE(orcid, :orcid)
                                    WHERE author_id = :author_id
                                """), {"orcid": auth_orcid, "author_id": author_uuid})
                        else:
                            author_uuid = conn.execute(text("""
                                INSERT INTO "Author" (display_name, orcid, openalex_id, openalex_synced_at)
                                VALUES (:name, :orcid, :openalex_id, :synced_at)
                                RETURNING author_id
                            """), {
                                "name": auth_name,
                                "orcid": auth_orcid,
                                "openalex_id": auth_openalex_id,
                                "synced_at": datetime.now(timezone.utc)
                            }).scalar()
                            
                        # Liên kết Author và Article
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
                        with engine.begin() as conn:
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
                        
                    with engine.begin() as conn:
                        sub_t_row = conn.execute(text("""
                            SELECT topic_id FROM "Topic" WHERE display_name = :name
                        """), {"name": t_name}).fetchone()
                        
                        if sub_t_row:
                            sub_topic_uuid = sub_t_row[0]
                        else:
                            sub_topic_uuid = conn.execute(text("""
                                INSERT INTO "Topic" (display_name, score)
                                VALUES (:name, :score)
                                RETURNING topic_id
                            """), {"name": t_name, "score": t_score}).scalar()
                            
                        conn.execute(text("""
                            INSERT INTO "Sub_Topic" (article_id, topic_id)
                            VALUES (:article_id, :topic_id)
                            ON CONFLICT DO NOTHING
                        """), {
                            "article_id": article_uuid,
                            "topic_id": sub_topic_uuid
                        })

                synced_works_count += 1
                
            print(f"  -> SUCCESS: Synced works for Journal: {journal_name}.")
        except Exception as e:
            print(f"  -> Request Exception for journal {journal_name}: {e}")
            
    print(f"\n[sync-works] Finished! Total synced works/articles: {synced_works_count}")


def cmd_stats_works():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        articles = conn.execute(text('SELECT COUNT(*) FROM "Article"')).scalar()
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
