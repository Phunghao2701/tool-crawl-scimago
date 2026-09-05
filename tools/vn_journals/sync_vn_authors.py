import argparse
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import os
import requests
import urllib.parse as urllib_parse
import urllib3

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from tools.vn_journals.import_one_journal_supabase import load_env, get_supabase_url

# Tắt các cảnh báo không an toàn khi dùng verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def safe_get(url: str, timeout: int = 25) -> requests.Response:
    parsed_url = urllib_parse.urlparse(url)
    query_params = urllib_parse.parse_qs(parsed_url.query)
    
    email = os.getenv("OPENALEX_EMAIL")
    api_key = os.getenv("OPENALEX_API_KEY")
    
    modified = False
    if email and "mailto" not in query_params:
        query_params["mailto"] = [email]
        modified = True
    if api_key and "api_key" not in query_params:
        query_params["api_key"] = [api_key]
        modified = True
        
    if modified:
        new_query = urllib_parse.urlencode(query_params, doseq=True)
        url = parsed_url._replace(query=new_query).geturl()

    headers = {"User-Agent": f"ScientificJournalETL/1.0 (mailto:{email or 'unknown@example.com'})"}

    retries = 3
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                print(f"  [API Limit] HTTP 429 (Too Many Requests). Sleeping 10s before retry {attempt+1}/{retries}...")
                time.sleep(10)
                continue
            return resp
        except Exception as e:
            print(f"  [Warning] Attempt {attempt+1}/{retries} failed: {e}. Retrying with verify=False...")
            try:
                resp = requests.get(url, headers=headers, timeout=timeout, verify=False)
                if resp.status_code == 429:
                    print(f"  [API Limit] HTTP 429 (Too Many Requests). Sleeping 10s...")
                    time.sleep(10)
                    continue
                return resp
            except Exception as e2:
                if attempt == retries - 1:
                    raise e2
                time.sleep(2)
    return requests.Response()  # Fallback empty response


def update_author_in_db(engine, author_id: int, data: dict):
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
                last_known_institution_id = :last_inst_id
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
            "author_id": author_id
        })



def _sync_authors_chunk(engine, chunk: list[str], is_orcid: bool, id_to_author: dict, orcid_to_author: dict, author_map: dict):
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

    time.sleep(0.1)
    response = safe_get(url, timeout=25)
    
    if getattr(response, "status_code", None) == 200:
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
    elif getattr(response, "status_code", None) == 429:
        print("[CRITICAL] OpenAlex API has blocked your IP (HTTP 429). Stop requested.")
        return -1
    else:
        print(f"  [ERROR] Failed to query chunk (HTTP {getattr(response, 'status_code', 'Unknown')})")
        
    return local_synced


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync OpenAlex Author statistics for authors in Vietnamese journals.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of authors to sync")
    args = parser.parse_args()

    load_env()
    supabase_url = get_supabase_url()
    engine = create_engine(supabase_url)

    # Lấy tác giả thuộc các bài báo trong tạp chí VN (country = 81)
    # Và chỉ lấy những tác giả CHƯA CÓ openalex_synced_at (hoặc h_index bị NULL)
    query = """
        SELECT DISTINCT a.author_id, a.orcid, a.openalex_id, a.display_name
        FROM "Author" a
        JOIN "Author_Article" aa ON a.author_id = aa.author_id
        JOIN "Article" art ON art.article_id = aa.article_id
        JOIN "Issue" i ON i.issue_id = art.issue_id
        JOIN "Volume" v ON v.volume_id = i.volume_id
        JOIN "Journal" j ON j.journal_id = v.journal_id
        WHERE j.country = 81
          AND a.h_index IS NULL
          AND (a.orcid IS NOT NULL OR a.openalex_id IS NOT NULL)
        ORDER BY a.author_id ASC
    """

    if args.limit:
        query += f" LIMIT {args.limit}"

    with engine.connect() as conn:
        authors = conn.execute(text(query)).fetchall()

    if not authors:
        print("[INFO] No Vietnamese journal authors need synchronization.")
        sys.exit(0)

    print(f"[sync-vn-authors] Starting bulk synchronization for {len(authors)} VN authors...")
    
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
    max_workers = 8
    
    # LUỒNG 1: Gom theo openalex_id
    if author_ids_to_query:
        print(f"[sync-vn-authors] Querying {len(author_ids_to_query)} authors by OpenAlex ID...")
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
                    
    # LUỒNG 2: Gom theo orcid
    if orcids_to_query:
        print(f"[sync-vn-authors] Querying {len(orcids_to_query)} authors by ORCID...")
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
                    
    # LUỒNG 3: Đánh dấu các tác giả không tìm thấy
    # do không có openalex_synced_at nên ta cập nhật h_index = -1 để đánh dấu đã quét mà không thấy
    not_found_authors = [v for v in author_map.values() if not v["found"]]
    if not_found_authors:
        print(f"[sync-vn-authors] Marking {len(not_found_authors)} authors not found in OpenAlex with h_index = -1...")
        with engine.begin() as conn:
            for item in not_found_authors:
                conn.execute(text("""
                    UPDATE "Author"
                    SET h_index = -1
                    WHERE author_id = :author_id
                """), {
                    "author_id": item["id"]
                })
                failed_count += 1

                
    print(f"\n[sync-vn-authors] Finished! Synced: {synced_count}, Not Found/Skipped: {failed_count}")


if __name__ == "__main__":
    main()
