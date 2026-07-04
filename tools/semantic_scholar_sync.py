"""
Enrich existing Article rows with Semantic Scholar metadata.

Usage examples:
  python tools/semantic_scholar_sync.py test-doi --doi 10.1038/nphys1170
  python tools/semantic_scholar_sync.py enrich-articles --limit 20
  python tools/semantic_scholar_sync.py enrich-articles --only-missing --limit 100
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:1234@localhost:5433/scientific_journal_db",
)
SEMANTIC_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
SEMANTIC_BASE_URL = os.getenv(
    "SEMANTIC_SCHOLAR_BASE_URL", "https://api.semanticscholar.org/graph/v1"
).rstrip("/")
SEMANTIC_RPS = max(float(os.getenv("SEMANTIC_SCHOLAR_RPS", "1") or 1), 0.1)
REQUEST_INTERVAL = 1.0 / SEMANTIC_RPS

FIELDS = ",".join([
    "paperId",
    "title",
    "abstract",
    "year",
    "citationCount",
    "referenceCount",
    "influentialCitationCount",
    "fieldsOfStudy",
    "publicationTypes",
    "externalIds",
    "tldr",
    "references.paperId",
    "references.title",
    "references.year",
    "references.externalIds",
    "references.authors",
])


def build_headers() -> dict:
    headers = {"User-Agent": "tool-crawl-scimago/semantic-sync"}
    if SEMANTIC_API_KEY:
        headers["x-api-key"] = SEMANTIC_API_KEY
    return headers


def normalize_doi(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    doi = raw.strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("https://dx.doi.org/", "").replace("http://dx.doi.org/", "")
    return doi.strip() or None


def fetch_paper_by_doi(session: requests.Session, doi: str) -> tuple[int, Optional[dict], str]:
    url = f"{SEMANTIC_BASE_URL}/paper/DOI:{doi}"
    resp = session.get(url, params={"fields": FIELDS}, timeout=30)
    if resp.status_code == 200:
        return 200, resp.json(), "ok"
    if resp.status_code == 404:
        return 404, None, "not_found"
    try:
        return resp.status_code, None, resp.text[:500]
    except Exception:
        return resp.status_code, None, "error"


def fetch_paper_by_title(session: requests.Session, title: str, year: Optional[int]) -> tuple[int, Optional[dict], str]:
    url = f"{SEMANTIC_BASE_URL}/paper/search"
    params = {
        "query": title,
        "limit": 1,
        "fields": FIELDS,
    }
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        try:
            return resp.status_code, None, resp.text[:500]
        except Exception:
            return resp.status_code, None, "error"

    data = resp.json() or {}
    rows = data.get("data") or []
    if not rows:
        return 404, None, "not_found"

    row = rows[0]
    row_year = row.get("year")
    if year and row_year and abs(int(row_year) - int(year)) > 1:
        return 404, None, "year_mismatch"
    return 200, row, "ok"


def extract_tldr(paper: dict) -> Optional[str]:
    tldr = paper.get("tldr")
    if isinstance(tldr, dict):
        return tldr.get("text")
    if isinstance(tldr, str):
        return tldr
    return None


def enrich_articles(limit: int, only_missing: bool, article_id: Optional[int] = None):
    engine = create_engine(DATABASE_URL)
    query = '''
        SELECT article_id, title, doi, publication_year
        FROM "Article"
        WHERE is_deleted = false
          AND title IS NOT NULL
    '''
    if article_id is not None:
        query += ' AND article_id = :article_id'
    if only_missing:
        query += ' AND (semantic_scholar_id IS NULL OR "references" IS NULL)'
    query += ' ORDER BY article_id ASC'
    if limit > 0:
        query += ' LIMIT :limit'

    stats = {
        "processed": 0,
        "updated": 0,
        "doi_match": 0,
        "title_match": 0,
        "not_found": 0,
        "error": 0,
    }

    session = requests.Session()
    session.headers.update(build_headers())

    with engine.connect() as conn:
        rows = conn.execute(text(query), {"limit": limit, "article_id": article_id}).fetchall()
        print(f"Found {len(rows)} article(s) to enrich.")

    for idx, row in enumerate(rows, start=1):
        article_id, title, doi, pub_year = row
        paper = None
        matched_by = None
        reason = None

        norm_doi = normalize_doi(doi)
        try:
            if norm_doi:
                status, paper, reason = fetch_paper_by_doi(session, norm_doi)
                if status == 200:
                    matched_by = "doi"
            if paper is None:
                status, paper, reason = fetch_paper_by_title(session, title, pub_year)
                if status == 200:
                    matched_by = "title"

            stats["processed"] += 1
            if paper is None:
                if reason in {"not_found", "year_mismatch"}:
                    stats["not_found"] += 1
                    print(f"[{idx}] NOT FOUND article_id={article_id} reason={reason} title={title[:80]}")
                else:
                    stats["error"] += 1
                    print(f"[{idx}] ERROR article_id={article_id} reason={reason} title={title[:80]}")
                time.sleep(REQUEST_INTERVAL)
                continue

            semantic_refs_payload = paper.get("references") or []
            with engine.begin() as update_conn:
                update_conn.execute(text('''
                    UPDATE "Article"
                    SET semantic_scholar_id = :paper_id,
                        citation_count = :citation_count,
                        semantic_influential_citation_count = :influential_citation_count,
                        semantic_external_ids = CAST(:external_ids AS JSONB),
                        semantic_tldr = :semantic_tldr,
                        abstract = CASE
                            WHEN NULLIF(:semantic_abstract, '') IS NOT NULL
                                 AND length(:semantic_abstract) > length(COALESCE(abstract, ''))
                            THEN :semantic_abstract
                            ELSE abstract
                        END,
                        "references" = CASE
                            WHEN ("references" IS NULL OR jsonb_typeof("references") <> 'array' OR jsonb_array_length("references") = 0)
                                 AND jsonb_array_length(CAST(:semantic_refs_payload AS JSONB)) > 0
                            THEN CAST(:semantic_refs_payload AS JSONB)
                            ELSE "references"
                        END,
                        reference_count = CASE
                            WHEN ("references" IS NULL OR jsonb_typeof("references") <> 'array' OR jsonb_array_length("references") = 0)
                                 AND jsonb_array_length(CAST(:semantic_refs_payload AS JSONB)) > 0
                            THEN jsonb_array_length(CAST(:semantic_refs_payload AS JSONB))
                            ELSE reference_count
                        END
                    WHERE article_id = :article_id
                '''), {
                    "article_id": article_id,
                    "paper_id": paper.get("paperId"),
                    "citation_count": paper.get("citationCount"),
                    "influential_citation_count": paper.get("influentialCitationCount"),
                    "external_ids": json.dumps(paper.get("externalIds", {}), ensure_ascii=False),
                    "semantic_tldr": extract_tldr(paper),
                    "semantic_abstract": paper.get("abstract"),
                    "semantic_refs_payload": json.dumps(semantic_refs_payload, ensure_ascii=False),
                })

            stats["updated"] += 1
            if matched_by == "doi":
                stats["doi_match"] += 1
            else:
                stats["title_match"] += 1
            print(f"[{idx}] UPDATED article_id={article_id} by={matched_by} paperId={paper.get('paperId')}")
        except requests.RequestException as exc:
            stats["processed"] += 1
            stats["error"] += 1
            print(f"[{idx}] REQUEST ERROR article_id={article_id}: {exc}")
        except Exception as exc:
            stats["processed"] += 1
            stats["error"] += 1
            print(f"[{idx}] ERROR article_id={article_id}: {exc}")
        finally:
            time.sleep(REQUEST_INTERVAL)

    print("\n=== Semantic Scholar enrich summary ===")
    for key, value in stats.items():
        print(f"{key}: {value}")


def test_doi(doi: str):
    session = requests.Session()
    session.headers.update(build_headers())
    status, paper, reason = fetch_paper_by_doi(session, normalize_doi(doi) or doi)
    print(f"status={status} reason={reason}")
    if paper:
        print(json.dumps(paper, ensure_ascii=False, indent=2)[:4000])


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_test = sub.add_parser("test-doi")
    p_test.add_argument("--doi", required=True)

    p_enrich = sub.add_parser("enrich-articles")
    p_enrich.add_argument("--limit", type=int, default=20)
    p_enrich.add_argument("--only-missing", action="store_true")
    p_enrich.add_argument("--article-id", type=int, default=None, help="Enrich one specific Article.article_id")

    args = parser.parse_args()
    if args.command == "test-doi":
        test_doi(args.doi)
    elif args.command == "enrich-articles":
        enrich_articles(args.limit, args.only_missing, args.article_id)


if __name__ == "__main__":
    main()
