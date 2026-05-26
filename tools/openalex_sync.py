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
    "postgresql+psycopg2://postgres:postgres@localhost:5433/scientific_journal_db",
)
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "academic-etl@example.com")


def get_headers():
    # Sử dụng Polite Pool của OpenAlex theo khuyến nghị chính thức
    return {
        "User-Agent": f"ScientificJournalETL/1.0 (mailto:{OPENALEX_EMAIL})"
    }


def sync_journals(limit: int):
    engine = create_engine(DATABASE_URL)
    
    # 1. Truy vấn các journal chưa đồng bộ OpenAlex hoặc đồng bộ lâu nhất
    query = """
        SELECT j.journal_id, j.display_name, 
               ARRAY_AGG(ji.issn) AS issns
        FROM journal j
        JOIN journal_issn ji ON j.journal_id = ji.journal_id
        WHERE j.openalex_synced_at IS NULL
        GROUP BY j.journal_id, j.display_name
        ORDER BY j.journal_id ASC
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
        issns = journal[2]
        
        print(f"[{idx}/{len(journals)}] Syncing: {display_name} (ISSNs: {', '.join(issns)})")
        
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


def main():
    parser = argparse.ArgumentParser(description="OpenAlex Sync Tool")
    sub = parser.add_subparsers(dest="command")
    
    # sync subcommand
    p_sync = sub.add_parser("sync", help="Sync data from OpenAlex")
    p_sync.add_argument("--limit", type=int, default=None, help="Limit number of journals to sync")
    
    # stats subcommand
    sub.add_parser("stats", help="Show OpenAlex synchronization stats")
    
    args = parser.parse_args()
    
    if args.command == "sync":
        sync_journals(args.limit)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
