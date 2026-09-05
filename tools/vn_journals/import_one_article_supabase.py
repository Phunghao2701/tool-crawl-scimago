from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.import_full_journal_supabase import (
    enrich_related_work_article,
    link_author_article as full_link_author_article,
    topic_ids_from_openalex_topic,
    upsert_article as full_upsert_article,
    upsert_article_citing_work,
    upsert_article_reference,
    upsert_author as full_upsert_author,
)
from tools.vn_journals.import_one_journal_supabase import (
    get_supabase_url,
    load_env,
    sync_identity_sequence,
)
from tools.vn_journals.paper_vn_affiliations import persist_article_authorship_institutions


def upsert_volume(conn, journal_id: int, volume: str | None, year: int | None) -> int | None:
    if not volume and not year:
        return None
    
    vol_num = None
    if volume:
        try:
            vol_num = int(volume)
        except ValueError:
            pass

    if vol_num is not None:
        row = conn.execute(
            text('SELECT "volume_id" FROM "Volume" WHERE "journal_id" = :journal_id AND "volume_number" = :volume_number LIMIT 1'),
            {"journal_id": journal_id, "volume_number": vol_num},
        ).fetchone()
    else:
        row = conn.execute(
            text('SELECT "volume_id" FROM "Volume" WHERE "journal_id" = :journal_id AND "volume_number" IS NULL AND "publication_year" = :year LIMIT 1'),
            {"journal_id": journal_id, "year": year},
        ).fetchone()
    if row:
        return int(row[0])

    sync_identity_sequence(conn, "Volume", "volume_id")
    return int(
        conn.execute(
            text(
                'INSERT INTO "Volume" ("journal_id", "volume_number", "publication_year") '
                'VALUES (:journal_id, :volume_number, :publication_year) RETURNING "volume_id"'
            ),
            {"journal_id": journal_id, "volume_number": vol_num, "publication_year": year},
        ).scalar_one()
    )


def upsert_issue(conn, volume_id: int | None, issue: str | None, year: int | None) -> int | None:
    if not volume_id and not issue:
        return None
    
    row = conn.execute(
        text('SELECT "issue_id" FROM "Issue" WHERE "volume_id" = :volume_id AND "issue_number" = :issue LIMIT 1'),
        {"volume_id": volume_id, "issue": issue},
    ).fetchone()
    if row:
        return int(row[0])

    sync_identity_sequence(conn, "Issue", "issue_id")
    return int(
        conn.execute(
            text(
                'INSERT INTO "Issue" ("volume_id", "issue_number", "publication_year") '
                'VALUES (:volume_id, :issue_number, :publication_year) RETURNING "issue_id"'
            ),
            {"volume_id": volume_id, "issue_number": issue, "publication_year": year},
        ).scalar_one()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview/import ONE VN article into Supabase")
    parser.add_argument("--json-file", type=Path, default=REPO_ROOT / "data" / "vietnam_journals" / "final" / "Acta_Mathematica_Vietnamica_openalex_final.json")
    parser.add_argument("--execute", action="store_true", help="Actually write to Supabase. Omit for dry-run preview.")
    args = parser.parse_args()

    load_env()
    
    if not args.json_file.exists():
        print(f"File not found: {args.json_file}")
        sys.exit(1)
        
    data = json.loads(args.json_file.read_text(encoding="utf-8"))
    
    if isinstance(data, list) and len(data) > 0:
        journal_payload = data[0]
        journal_code = journal_payload.get("journal", {}).get("code")
        articles = journal_payload.get("articles", [])
    else:
        print("Invalid JSON structure")
        sys.exit(1)

    if not articles:
        print("No articles found in JSON.")
        sys.exit(0)

    # Pick the first article to test
    article = articles[0]
    
    # 1. Preview Mapping
    print("[PREVIEW] VN Article -> Supabase mapping")
    preview_data = {
        "Article": {
            "title": article.get("title"),
            "doi": article.get("doi"),
            "publication_year": article.get("publication_year"),
            "cited_by_count": article.get("openalex", {}).get("cited_by_count"),
            "reference_count": article.get("openalex", {}).get("referenced_works_count"),
            "Volume_number": article.get("volume"),
            "Issue_number": article.get("issue"),
        },
        "Authors": [
            {
                "display_name": a.get("name"),
                "orcid": a.get("orcid"),
                "openalex_id": a.get("openalex_author_id") or a.get("id")
            }
            for a in article.get("authors", [])
        ]
    }
    print(json.dumps(preview_data, ensure_ascii=False, indent=2))

    if not args.execute:
        print("\n[DRY RUN] No database changes were made. Add --execute to import this article.")
        return

    supabase_url = get_supabase_url()
    engine = create_engine(supabase_url)
    
    with engine.begin() as conn:
        # Find Journal ID
        journal_row = conn.execute(
            text('SELECT "journal_id" FROM "Journal" WHERE "source_id" = :code OR "source_id" LIKE :code_pattern LIMIT 1'),
            {"code": journal_code, "code_pattern": f"%{journal_code}%"}
        ).fetchone()
        
        if not journal_row:
            # Fallback check by name
            journal_row = conn.execute(
                text('SELECT "journal_id" FROM "Journal" WHERE "display_name" = :name LIMIT 1'),
                {"name": journal_payload.get("journal", {}).get("name_en")}
            ).fetchone()
            
        if not journal_row:
            print("ERROR: Journal must be imported first! Could not find journal in DB.")
            sys.exit(1)
            
        journal_id = int(journal_row[0])
        print(f"Found Journal ID: {journal_id}")

        vol_id = upsert_volume(conn, journal_id, article.get("volume"), article.get("publication_year"))
        issue_id = upsert_issue(conn, vol_id, article.get("issue"), article.get("publication_year"))

        oa_data = article.get("openalex") or {}
        author_cache = {}
        keyword_cache = {}
        topic_cache = {}
        primary_topic_ids = topic_ids_from_openalex_topic(conn, oa_data.get("primary_topic"), topic_cache)
        article_id = full_upsert_article(conn, article, issue_id, primary_topic_ids[0] if primary_topic_ids else None, is_vn_journal=True)
        enrichment_caches = {
            "author_cache": author_cache,
            "keyword_cache": keyword_cache,
            "topic_cache": topic_cache,
            "openalex_work_by_doi": {},
            "openalex_work_by_id": {},
            "related_article_by_work": {},
        }
        enrichment_stats = {
            "related_dois_seen": 0,
            "related_works_seen": 0,
            "related_works_fetched": 0,
            "related_articles_upserted": 0,
            "related_works_missing": 0,
            "related_dois_missing": 0,
            "institutions_found": 0,
            "institution_links_inserted": 0,
        }

        for citing_work in oa_data.get("citing_works", []) or []:
            citing_article_id = enrich_related_work_article(conn, citing_work, enrichment_caches, enrichment_stats)
            upsert_article_citing_work(conn, article_id, citing_work, citing_article_id)

        reference_items = oa_data.get("referenced_works_enriched", []) or []
        if reference_items:
            for ref_idx, reference_work in enumerate(reference_items):
                referenced_article_id = enrich_related_work_article(conn, reference_work, enrichment_caches, enrichment_stats)
                upsert_article_reference(conn, article_id, reference_work, ref_idx, referenced_article_id)
        else:
            for ref_idx, reference_id in enumerate(oa_data.get("referenced_works", []) or []):
                upsert_article_reference(conn, article_id, {"work_id": reference_id}, ref_idx)
        
        author_ids = []
        for a in article.get("authors", []):
            auth_id = full_upsert_author(conn, a, author_cache)
            author_ids.append(auth_id)
            author_pos = a.get("author_position")
            full_link_author_article(conn, auth_id, article_id, author_pos)
        affil_stats = persist_article_authorship_institutions(
            conn,
            article_id,
            article.get("publication_year"),
            article.get("authors", []) or [],
            author_cache,
        )
        enrichment_stats["institutions_found"] += affil_stats.institutions_found
        enrichment_stats["institution_links_inserted"] += affil_stats.institution_links_inserted

    print(f"\n[OK] Imported one article into Supabase")
    print(
        "Related DOI enrichment: "
        f"seen={enrichment_stats['related_works_seen']}, "
        f"fetched={enrichment_stats['related_works_fetched']}, "
        f"upserted={enrichment_stats['related_articles_upserted']}, "
        f"missing_or_failed={enrichment_stats['related_works_missing']}, "
        f"institution_links_inserted={enrichment_stats['institution_links_inserted']}"
    )
    print(json.dumps({"journal_id": journal_id, "volume_id": vol_id, "issue_id": issue_id, "article_id": article_id, "author_ids": author_ids}, indent=2))


if __name__ == "__main__":
    main()
