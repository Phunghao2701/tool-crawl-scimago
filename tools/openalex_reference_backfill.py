"""
Backfill Article.references and Article.reference_count from OpenAlex by DOI.

This tool is intentionally focused on merged reference detail data:
- references      <- OpenAlex work.referenced_works
- reference_count <- OpenAlex work.referenced_works_count, fallback len(referenced_works)
- citation_count        <- OpenAlex work.cited_by_count when present

Examples:
  python tools/openalex_reference_backfill.py --limit 100
  python tools/openalex_reference_backfill.py --limit 0
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

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
    return doi or None


def fetch_openalex_work(session: requests.Session, doi: str) -> tuple[int, Optional[dict], str]:
    url = f"https://api.openalex.org/works/https://doi.org/{doi}"
    params = {"mailto": OPENALEX_EMAIL}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY

    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 200:
        return 200, resp.json(), "ok"
    if resp.status_code == 404:
        return 404, None, "not_found"
    return resp.status_code, None, resp.text[:500]


def select_target_articles(conn, limit: int, min_year: Optional[int]):
    query = '''
        SELECT article_id, title, doi
        FROM "Article"
        WHERE is_deleted = false
          AND doi IS NOT NULL
          AND trim(doi) <> ''
          AND (:min_year IS NULL OR publication_year >= :min_year)
          AND (
              "references" IS NULL
              OR jsonb_typeof("references") <> 'array'
              OR jsonb_array_length("references") = 0
              OR reference_count IS NULL
              OR reference_count = 0
          )
        ORDER BY article_id ASC
    '''
    if limit > 0:
        query += " LIMIT :limit"
    return conn.execute(text(query), {"limit": limit, "min_year": min_year}).fetchall()


def backfill_openalex_references(limit: int, min_year: Optional[int]):
    engine = create_engine(DATABASE_URL)
    session = requests.Session()
    session.headers.update({"User-Agent": "tool-crawl-scimago/openalex-reference-backfill"})

    stats = {
        "selected": 0,
        "processed": 0,
        "updated_with_refs": 0,
        "updated_empty_refs": 0,
        "not_found": 0,
        "errors": 0,
        "skipped_no_doi": 0,
    }

    with engine.connect() as conn:
        rows = select_target_articles(conn, limit, min_year)
    stats["selected"] = len(rows)
    min_year_msg = f" publication_year>={min_year}" if min_year else ""
    print(f"Found {len(rows)} article(s) needing OpenAlex reference backfill{min_year_msg}.")

    for idx, row in enumerate(rows, start=1):
        article_id, title, raw_doi = row
        doi = normalize_doi(raw_doi)
        if not doi:
            stats["skipped_no_doi"] += 1
            continue

        try:
            status, work, reason = fetch_openalex_work(session, doi)
            stats["processed"] += 1
            if status != 200 or not work:
                if reason == "not_found":
                    stats["not_found"] += 1
                    print(f"[{idx}] NOT FOUND article_id={article_id} doi={doi}")
                else:
                    stats["errors"] += 1
                    print(f"[{idx}] ERROR article_id={article_id} status={status} reason={reason}")
                time.sleep(REQUEST_INTERVAL)
                continue

            refs = work.get("referenced_works") or []
            ref_count = work.get("referenced_works_count")
            if ref_count is None:
                ref_count = len(refs)

            with engine.begin() as conn:
                conn.execute(text('''
                    UPDATE "Article"
                    SET "references" = CAST(:references_json AS JSONB),
                        reference_count = :reference_count,
                        citation_count = COALESCE(:cited_by_count, citation_count)
                    WHERE article_id = :article_id
                '''), {
                    "article_id": article_id,
                    "references_json": json.dumps(refs, ensure_ascii=False),
                    "reference_count": ref_count,
                    "cited_by_count": work.get("cited_by_count"),
                })

            if refs:
                stats["updated_with_refs"] += 1
                print(f"[{idx}] UPDATED article_id={article_id} refs={len(refs)} doi={doi}")
            else:
                stats["updated_empty_refs"] += 1
                print(f"[{idx}] UPDATED EMPTY article_id={article_id} refs=0 doi={doi}")
        except requests.RequestException as exc:
            stats["processed"] += 1
            stats["errors"] += 1
            print(f"[{idx}] REQUEST ERROR article_id={article_id} doi={doi}: {exc}")
        except Exception as exc:
            stats["processed"] += 1
            stats["errors"] += 1
            print(f"[{idx}] ERROR article_id={article_id} doi={doi}: {exc}")
        finally:
            time.sleep(REQUEST_INTERVAL)

    print("\n=== OpenAlex reference backfill summary ===")
    for key, value in stats.items():
        print(f"{key}: {value}")


def main():
    parser = argparse.ArgumentParser(description="Backfill Article references from OpenAlex by DOI")
    parser.add_argument("--limit", type=int, default=100, help="Max articles to process; 0 means all")
    parser.add_argument("--min-year", type=int, default=None, help="Only process articles from this publication year onward")
    args = parser.parse_args()
    backfill_openalex_references(args.limit, args.min_year)


if __name__ == "__main__":
    main()
