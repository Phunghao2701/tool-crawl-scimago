"""
OpenAlex Sync Tool
Usage:
  python tools/openalex_sync.py sync --limit 10
  python tools/openalex_sync.py stats
"""

import argparse
import os
import sys
import time
import requests
from datetime import datetime
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


def scrape_scimago_scope_with_driver(driver, source_id: str, is_first: bool):
    if not source_id:
        return None, is_first
    
    url = f"https://www.scimagojr.com/journalsearch.php?q={source_id}&tip=sid&clean=0"
    try:
        from selenium.webdriver.common.by import By
        driver.get(url)
        # Đợi cho trang load và vượt qua Cloudflare Turnstile
        # Lần đầu tiên chờ lâu hơn để cookie session ổn định, các lần sau load nhanh tức thì
        wait_time = 8.0 if is_first else 1.5
        time.sleep(wait_time)
        
        body_text = driver.find_element(By.TAG_NAME, "body").text
        lines = [line.strip() for line in body_text.split("\n")]
        
        scope_text = ""
        for i, line in enumerate(lines):
            if line == "Scope":
                if i + 1 < len(lines):
                    scope_text = lines[i+1]
                break
        
        if scope_text:
            return scope_text, False
    except Exception as e:
        print(f"    [Scope Scrape Error] Failed to scrape Scope for source_id {source_id}: {e}")
    return None, is_first


def generate_fallback_scope(source_data: dict):
    topics = source_data.get("topics", [])
    topic_names = [t.get("display_name") for t in topics if t.get("display_name")]
    if topic_names:
        return f"This journal covers topics and research areas in: {', '.join(topic_names[:5])}."
    return None


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

    # Khởi tạo Selenium Chrome Driver
    print("\nInitializing Selenium Chrome Driver for Scimago Scope scraping...")
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    is_first_scimago_request = True

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
                        "synced_at": datetime.utcnow(),
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
                            
                            # Cào Scope trực tiếp từ Scimago
                            scope = None
                            if source_id:
                                print(f"  -> Scraping Scope from Scimago (source_id: {source_id})...")
                                scope, is_first_scimago_request = scrape_scimago_scope_with_driver(
                                    driver, source_id, is_first_scimago_request
                                )
                            
                            if not scope:
                                print("     (Scimago Scope not found. Fallback to OpenAlex topics)")
                                scope = generate_fallback_scope(source_data)
                            
                            # Cập nhật thông tin vào DB
                            with engine.begin() as conn:
                                conn.execute(text("""
                                    UPDATE "Journal"
                                    SET openalex_id = :openalex_id,
                                        homepage_url = :homepage_url,
                                        works_count = :works_count,
                                        cited_by_count = :cited_by_count,
                                        scope = :scope,
                                        openalex_synced_at = :synced_at
                                    WHERE journal_id = :journal_id
                                """), {
                                    "openalex_id": openalex_id,
                                    "homepage_url": homepage_url,
                                    "works_count": works_count,
                                    "cited_by_count": cited_by_count,
                                    "scope": scope,
                                    "synced_at": datetime.utcnow(),
                                    "journal_id": journal_id
                                })
                            
                            print(f"  -> SUCCESS: OpenAlex ID={openalex_id}, Works={works_count}, Cites={cited_by_count}")
                            if scope:
                                preview_scope = scope[:60] + "..." if len(scope) > 63 else scope
                                print(f"     Scope: {preview_scope}")
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
                        "synced_at": datetime.utcnow(),
                        "journal_id": journal_id
                    })
                print("  -> FAILED: Journal not found or API error on all ISSNs.")
                failed_count += 1
    finally:
        print("\nClosing Selenium Chrome Driver...")
        try:
            driver.quit()
        except Exception:
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
        print(f"  Synced with OpenAlex:    {synced:,} ({synced/total*100:.1f}% if total > 0 else 0)")
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
            j.scope AS openalex_scope,
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
            raw_dict["Scope"] = row.openalex_scope
            
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
            lambda c: c.lower() == "scope",
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


def main():
    parser = argparse.ArgumentParser(description="OpenAlex Sync Tool")
    sub = parser.add_subparsers(dest="command")
    
    # sync subcommand
    p_sync = sub.add_parser("sync", help="Sync data from OpenAlex")
    p_sync.add_argument("--limit", type=int, default=None, help="Limit number of journals to sync")
    
    # stats subcommand
    sub.add_parser("stats", help="Show OpenAlex synchronization stats")

    # export subcommand
    p_export = sub.add_parser("export", help="Export enriched journals to CSV")
    p_export.add_argument("--output", default="data/enriched_journals.csv", help="Output CSV file path")
    p_export.add_argument("--limit", type=int, default=20, help="Number of preview records on screen")
    
    args = parser.parse_args()
    
    if args.command == "sync":
        sync_journals(args.limit)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "export":
        cmd_export(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
