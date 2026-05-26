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


def sync_journals(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Truy vấn các journal chưa đồng bộ OpenAlex trực tiếp từ bảng journal
    query = """
        SELECT journal_id, display_name, issn
        FROM journal
        WHERE openalex_synced_at IS NULL
        ORDER BY journal_id ASC
    """
    if limit:
        query += f" LIMIT {limit}"

    with engine.connect() as conn:
        journals = conn.execute(text(query)).fetchall()

    if not journals:
        print("[INFO] No journals need synchronization.")
        return

    print(f"[sync] Starting synchronization for {len(journals)} journals...")
    
    synced_count = 0
    failed_count = 0
    
    for idx, journal in enumerate(journals, 1):
        journal_id = journal[0]
        display_name = journal[1]
        issn_str = journal[2] or ""
        issns = split_issns(issn_str)
        
        print(f"[{idx}/{len(journals)}] Syncing: {display_name} (ISSNs: {', '.join(issns) if issns else 'None'})")
        
        if not issns:
            print("  -> SKIPPED: No valid ISSNs found for this journal.")
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE journal
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
                        
                        # Cập nhật thông tin vào DB
                        with engine.begin() as conn:
                            conn.execute(text("""
                                UPDATE journal
                                SET openalex_id = :openalex_id,
                                    homepage_url = :homepage_url,
                                    works_count = :works_count,
                                    cited_by_count = :cited_by_count,
                                    openalex_synced_at = :synced_at
                                WHERE journal_id = :journal_id
                            """), {
                                "openalex_id": openalex_id,
                                "homepage_url": homepage_url,
                                "works_count": works_count,
                                "cited_by_count": cited_by_count,
                                "synced_at": datetime.utcnow(),
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
                    UPDATE journal
                    SET openalex_synced_at = :synced_at
                    WHERE journal_id = :journal_id
                """), {
                    "synced_at": datetime.utcnow(),
                    "journal_id": journal_id
                })
            print("  -> FAILED: Journal not found or API error on all ISSNs.")
            failed_count += 1
            
    print(f"\n[sync] Finished! Synced: {synced_count}, Failed/Not found: {failed_count}")


def cmd_stats(args):
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM journal")).scalar()
        synced = conn.execute(text("SELECT COUNT(*) FROM journal WHERE openalex_id IS NOT NULL")).scalar()
        unsynced = conn.execute(text("SELECT COUNT(*) FROM journal WHERE openalex_synced_at IS NULL")).scalar()
        
        print("\n[OpenAlex Sync Stats]")
        print(f"  Total journals in DB:    {total:,}")
        print(f"  Synced with OpenAlex:    {synced:,} ({synced/total*100:.1f}% if total > 0 else 0)")
        print(f"  Pending sync:            {unsynced:,}")
        
        if synced > 0:
            print("\n[Latest Synced Journals Sample]")
            rows = conn.execute(text("""
                SELECT display_name, openalex_id, works_count, cited_by_count, homepage_url
                FROM journal
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
    engine = create_engine(DATABASE_URL)
    
    print("[export] Fetching enriched data from PostgreSQL...")
    query = """
        SELECT 
            j.source_id AS scimago_id,
            j.display_name AS title,
            j.issn,
            j.type,
            p.display_name AS publisher,
            z_c.name AS country,
            z_r.name AS region,
            j.is_open_access,
            j.is_oa_diamond,
            j.homepage_url,
            j.openalex_id,
            j.works_count,
            j.cited_by_count,
            (SELECT value_int FROM journal_ranking jr 
             JOIN ranking_metric m ON jr.metric_id = m.metric_id 
             WHERE jr.journal_id = j.journal_id AND m.code = 'RANK' 
             ORDER BY jr.year DESC LIMIT 1) AS last_rank,
            (SELECT value_float FROM journal_ranking jr 
             JOIN ranking_metric m ON jr.metric_id = m.metric_id 
             WHERE jr.journal_id = j.journal_id AND m.code = 'SJR' 
             ORDER BY jr.year DESC LIMIT 1) AS last_sjr,
            (SELECT value_txt FROM journal_ranking jr 
             JOIN ranking_metric m ON jr.metric_id = m.metric_id 
             WHERE jr.journal_id = j.journal_id AND m.code = 'SJR_BEST_QUARTILE' 
             ORDER BY jr.year DESC LIMIT 1) AS last_best_quartile
        FROM journal j
        LEFT JOIN publisher p ON j.publisher_id = p.publisher_id
        LEFT JOIN zone z_c ON j.country_id = z_c.zone_id
        LEFT JOIN zone z_r ON j.region_id = z_r.zone_id
        ORDER BY last_sjr DESC NULLS LAST, last_rank ASC NULLS LAST
    """
    
    try:
        df = pd.read_sql(query, engine)
        
        # In ra màn hình mẫu preview
        limit = args.limit or 10
        print(f"\n[export] Enriched Journals (Top {limit} by SJR):")
        cols_to_print = ["title", "issn", "publisher", "country", "last_sjr", "last_best_quartile", "works_count", "cited_by_count"]
        print(df[cols_to_print].head(limit).to_string(index=False))
        
        # Lưu ra file CSV
        output_file = args.output
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n[OK] Exported {len(df)} enriched records to CSV: {output_file}")
        
        # Xuất ra file Excel (.xlsx)
        excel_file = os.path.splitext(output_file)[0] + ".xlsx"
        try:
            df.to_excel(excel_file, index=False)
            print(f"[OK] Exported {len(df)} enriched records to Excel: {excel_file}")
        except (ImportError, ModuleNotFoundError):
            print("[INFO] Installing openpyxl for Excel output...")
            try:
                import subprocess
                subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl"])
                # Import lại pandas engine sau khi cài đặt openpyxl
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
