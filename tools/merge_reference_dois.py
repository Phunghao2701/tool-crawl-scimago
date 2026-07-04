"""
Merge references from OpenAlex and Semantic Scholar into a single list of DOIs.
It resolves OpenAlex Work IDs to DOIs via the OpenAlex API and merges them with Semantic DOIs.

Usage:
  python tools/merge_reference_dois.py --limit 100
  python tools/merge_reference_dois.py --limit 0
"""

import argparse
import json
import os
import sys
import time
from typing import Optional, List, Set

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db",
)
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "academic-etl@example.com").strip()
OPENALEX_API_KEY = os.getenv("OPENALEX_API_KEY", "").strip()
OPENALEX_RPS = max(float(os.getenv("OPENALEX_RPS", "1") or 1), 0.1)
REQUEST_INTERVAL = 1.0 / OPENALEX_RPS


def normalize_doi(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    doi = raw.strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("https://dx.doi.org/", "").replace("http://dx.doi.org/", "")
    doi = doi.strip().strip("/")
    doi = doi.lower()
    if doi:
        return f"https://doi.org/{doi}"
    return None


def get_dois_from_semantic(refs: list) -> Set[str]:
    dois = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ext_ids = ref.get("externalIds") or {}
        raw_doi = ext_ids.get("DOI")
        norm = normalize_doi(raw_doi)
        if norm:
            dois.add(norm)
    return dois


def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_dois_from_openalex(session: requests.Session, openalex_urls: list) -> Set[str]:
    # Extract IDs: "https://openalex.org/W12345" -> "W12345"
    oa_ids = []
    for url in openalex_urls:
        if isinstance(url, str):
            parts = url.split("/")
            if parts:
                oa_ids.append(parts[-1])
    
    if not oa_ids:
        return set()

    dois = set()
    # OpenAlex allows up to 50 items in a filter separated by |
    for chunk in chunk_list(oa_ids, 50):
        filter_str = "openalex:" + "|".join(chunk)
        url = "https://api.openalex.org/works"
        params = {
            "filter": filter_str,
            "select": "id,doi",
            "per-page": 50,
            "mailto": OPENALEX_EMAIL
        }
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
            
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                for work in results:
                    norm = normalize_doi(work.get("doi"))
                    if norm:
                        dois.add(norm)
            time.sleep(REQUEST_INTERVAL)
        except Exception as e:
            print(f"    [WARN] OpenAlex DOI resolution failed for chunk: {e}")
            time.sleep(REQUEST_INTERVAL)
            
    return dois


def process_merges(limit: int, article_id: Optional[int] = None):
    engine = create_engine(DATABASE_URL)
    session = requests.Session()
    session.headers.update({"User-Agent": "tool-crawl-scimago/merge-dois"})

    # Rebuild references from the existing merged reference payload.
    query = '''
        SELECT article_id, "references"
        FROM "Article"
        WHERE is_deleted = false
          AND (:article_id IS NULL OR article_id = :article_id)
          AND jsonb_typeof("references") = 'array'
          AND jsonb_array_length("references") > 0
        ORDER BY article_id ASC
    '''
    if limit > 0:
        query += " LIMIT :limit"

    with engine.connect() as conn:
        rows = conn.execute(text(query), {"limit": limit, "article_id": article_id}).fetchall()

    print(f"Found {len(rows)} article(s) to process DOI merge.")

    stats = {
        "processed": 0,
        "merged": 0,
        "empty_doi": 0,
        "total_dois_extracted": 0,
    }

    for idx, row in enumerate(rows, start=1):
        article_id, refs = row
        stats["processed"] += 1

        refs_list = refs if isinstance(refs, list) else []

        # Resolve any OpenAlex work IDs to DOI values. Existing DOI strings are preserved.
        direct_dois = {normalize_doi(x) for x in refs_list if isinstance(x, str) and "10." in x.lower()}
        direct_dois.discard(None)
        oa_dois = get_dois_from_openalex(session, refs_list)

        # Merge
        merged = direct_dois.union(oa_dois)
        merged_list = list(merged)
        

        with engine.begin() as conn:
            conn.execute(text('''
                UPDATE "Article"
                SET "references" = CAST(:final_refs AS JSONB),
                    reference_count = :count
                WHERE article_id = :article_id
            '''), {
                "article_id": article_id,
                "final_refs": json.dumps(merged_list, ensure_ascii=False),
                "count": len(merged_list),
            })

        if merged_list:
            stats["merged"] += 1
            stats["total_dois_extracted"] += len(merged_list)
            print(f"[{idx}] UPDATED article_id={article_id} | DOIs: {len(merged_list)}")
        else:
            stats["empty_doi"] += 1
            print(f"[{idx}] UPDATED article_id={article_id} | NO DOIs FOUND")

    print("\n=== Reference DOIs Merge Summary ===")
    for k, v in stats.items():
        print(f"{k}: {v}")


def main():
    parser = argparse.ArgumentParser(description="Merge OpenAlex and Semantic references to unique DOIs")
    parser.add_argument("--limit", type=int, default=100, help="Max articles to process; 0 means all")
    parser.add_argument("--article-id", type=int, default=None, help="Process one specific Article.article_id")
    args = parser.parse_args()
    process_merges(args.limit, args.article_id)


if __name__ == "__main__":
    main()
