from __future__ import annotations

import argparse
import enum
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.import_one_journal_supabase import (
    get_supabase_url,
    load_env,
)
from tools.vn_journals.paper_vn_affiliations import (
    persist_article_authorship_institutions,
)
from tools.vn_journals.paper_vn_article_metadata import (
    normalize_doi,
    normalize_work_id,
)
from tools.vn_journals.import_full_journal_supabase import (
    normalize_relationship_work,
    openalex_work_to_article,
    upsert_article,
    topic_ids_from_openalex_topic,
    upsert_keyword,
    link_keyword_article,
    link_sub_topic,
    upsert_author,
    link_author_article,
    doi_lookup_sql,
    doi_lookup_params,
    make_reference_key,
)


class FetchOutcome(enum.Enum):
    FOUND = 1
    NOT_FOUND = 2
    TRANSIENT_FAILURE = 3
    INVALID_RESPONSE = 4


def table_exists(conn, table_name: str) -> bool:
    if conn.engine.dialect.name == "sqlite":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table_name"
                ),
                {"table_name": table_name},
            ).scalar()
        )
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :table_name)"
            ),
            {"table_name": table_name},
        ).scalar()
    )


def get_existing_article_id(conn, openalex_id: str | None, doi: str | None) -> int | None:
    if openalex_id:
        row = conn.execute(
            text('SELECT "article_id" FROM "Article" WHERE "openalex_id" = :openalex_id OR "openalex_id" = :short_id LIMIT 1'),
            {"openalex_id": openalex_id, "short_id": openalex_id.rsplit("/", 1)[-1]},
        ).fetchone()
        if row:
            return int(row[0])
    if doi:
        row = conn.execute(
            text(f'SELECT "article_id" FROM "Article" WHERE {doi_lookup_sql()} LIMIT 1'),
            doi_lookup_params(doi),
        ).fetchone()
        if row:
            return int(row[0])
    return None


def update_pending_relationship_ids(conn, article_id: int, openalex_id: str | None, doi: str | None) -> None:
    """Resolve child article links inside relationship tables when child is crawled/resolved."""
    if openalex_id:
        short_id = openalex_id.rsplit("/", 1)[-1]
        conn.execute(
            text(
                'UPDATE "Article_Citing_Work" SET "citing_article_id" = :article_id, "updated_at" = CURRENT_TIMESTAMP '
                'WHERE ("openalex_work_id" = :openalex_id OR "openalex_work_id" = :short_id) AND "citing_article_id" IS NULL'
            ),
            {"article_id": article_id, "openalex_id": openalex_id, "short_id": short_id},
        )
        conn.execute(
            text(
                'UPDATE "Article_Reference" SET "referenced_article_id" = :article_id, "updated_at" = CURRENT_TIMESTAMP '
                'WHERE ("openalex_work_id" = :openalex_id OR "openalex_work_id" = :short_id) AND "referenced_article_id" IS NULL'
            ),
            {"article_id": article_id, "openalex_id": openalex_id, "short_id": short_id},
        )
    if doi:
        conn.execute(
            text(
                'UPDATE "Article_Citing_Work" SET "citing_article_id" = :article_id, "updated_at" = CURRENT_TIMESTAMP '
                'WHERE lower(trim("doi")) = lower(trim(:doi)) AND "citing_article_id" IS NULL'
            ),
            {"article_id": article_id, "doi": doi},
        )
        conn.execute(
            text(
                'UPDATE "Article_Reference" SET "referenced_article_id" = :article_id, "updated_at" = CURRENT_TIMESTAMP '
                'WHERE lower(trim("doi")) = lower(trim(:doi)) AND "referenced_article_id" IS NULL'
            ),
            {"article_id": article_id, "doi": doi},
        )


def upsert_article_citing_work(conn, article_id: int, work: dict[str, Any], citing_article_id: int | None = None) -> None:
    payload = normalize_relationship_work(work)
    if not payload.get("openalex_work_id"):
        return
    payload["article_id"] = article_id
    payload["citing_article_id"] = citing_article_id
    conn.execute(
        text(
            'INSERT INTO "Article_Citing_Work" ('
            '"article_id", "openalex_work_id", "doi", "title", "publication_year", "source_name", '
            '"source_url", "landing_url", "pdf_url", "cited_by_count", "type", "authors", "raw", "citing_article_id", "updated_at") '
            'VALUES (:article_id, :openalex_work_id, :doi, :title, :publication_year, :source_name, '
            ':source_url, :landing_url, :pdf_url, :cited_by_count, :type, CAST(:authors AS JSONB), CAST(:raw AS JSONB), :citing_article_id, CURRENT_TIMESTAMP) '
            'ON CONFLICT ("article_id", "openalex_work_id") DO UPDATE SET '
            '"doi" = EXCLUDED."doi", "title" = EXCLUDED."title", "publication_year" = EXCLUDED."publication_year", '
            '"source_name" = EXCLUDED."source_name", "source_url" = EXCLUDED."source_url", '
            '"landing_url" = EXCLUDED."landing_url", "pdf_url" = EXCLUDED."pdf_url", '
            '"cited_by_count" = EXCLUDED."cited_by_count", "type" = EXCLUDED."type", '
            '"authors" = EXCLUDED."authors", "raw" = EXCLUDED."raw", '
            '"citing_article_id" = COALESCE("Article_Citing_Work"."citing_article_id", EXCLUDED."citing_article_id"), '
            '"updated_at" = CURRENT_TIMESTAMP'
        ),
        payload,
    )


def upsert_article_reference(conn, article_id: int, work: dict[str, Any], index: int, referenced_article_id: int | None = None) -> None:
    payload = normalize_relationship_work(work)
    payload["article_id"] = article_id
    payload["reference_key"] = make_reference_key(work, index)
    payload["semantic_scholar_id"] = work.get("semantic_scholar_id") or work.get("paperId")
    payload["referenced_article_id"] = referenced_article_id
    conn.execute(
        text(
            'INSERT INTO "Article_Reference" ('
            '"article_id", "reference_key", "openalex_work_id", "semantic_scholar_id", "doi", "title", '
            '"publication_year", "source_name", "source_url", "landing_url", "pdf_url", "cited_by_count", '
            '"type", "authors", "raw", "referenced_article_id", "updated_at") '
            'VALUES (:article_id, :reference_key, :openalex_work_id, :semantic_scholar_id, :doi, :title, '
            ':publication_year, :source_name, :source_url, :landing_url, :pdf_url, :cited_by_count, '
            ':type, CAST(:authors AS JSONB), CAST(:raw AS JSONB), :referenced_article_id, CURRENT_TIMESTAMP) '
            'ON CONFLICT ("article_id", "reference_key") DO UPDATE SET '
            '"openalex_work_id" = EXCLUDED."openalex_work_id", "semantic_scholar_id" = EXCLUDED."semantic_scholar_id", '
            '"doi" = EXCLUDED."doi", "title" = EXCLUDED."title", "publication_year" = EXCLUDED."publication_year", '
            '"source_name" = EXCLUDED."source_name", "source_url" = EXCLUDED."source_url", '
            '"landing_url" = EXCLUDED."landing_url", "pdf_url" = EXCLUDED."pdf_url", '
            '"cited_by_count" = EXCLUDED."cited_by_count", "type" = EXCLUDED."type", '
            '"authors" = EXCLUDED."authors", "raw" = EXCLUDED."raw", '
            '"referenced_article_id" = COALESCE("Article_Reference"."referenced_article_id", EXCLUDED."referenced_article_id"), '
            '"updated_at" = CURRENT_TIMESTAMP'
        ),
        payload,
    )


def execute_openalex_request(url: str, params: dict[str, str]) -> requests.Response:
    return requests.get(url, params=params, timeout=30)


def fetch_openalex_params() -> dict[str, str]:
    params: dict[str, str] = {}
    mailto = os.getenv("OPENALEX_EMAIL")
    api_key = os.getenv("OPENALEX_API_KEY")
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    return params


def fetch_work_with_outcome(
    identifier: str,
    is_doi: bool,
    max_retries: int,
    delay: float,
    api_params: dict[str, str],
) -> tuple[FetchOutcome, dict[str, Any] | None]:
    """Fetch OpenAlex work details, with retries and exponential backoff for transient errors."""
    if is_doi:
        encoded_doi = requests.utils.quote(identifier, safe="")
        url = f"https://api.openalex.org/works/https://doi.org/{encoded_doi}"
    else:
        short_id = identifier.rsplit("/", 1)[-1]
        url = f"https://api.openalex.org/works/{short_id}"

    current_delay = delay
    for attempt in range(max_retries + 1):
        try:
            response = execute_openalex_request(url, api_params)
            if response.status_code == 200:
                try:
                    payload = response.json()
                    if isinstance(payload, dict) and ("id" in payload or "doi" in payload):
                        return FetchOutcome.FOUND, payload
                    else:
                        return FetchOutcome.INVALID_RESPONSE, None
                except ValueError:
                    return FetchOutcome.INVALID_RESPONSE, None
            elif response.status_code == 404:
                return FetchOutcome.NOT_FOUND, None
            elif response.status_code in (429, 500, 502, 503, 504):
                if attempt < max_retries:
                    time.sleep(current_delay)
                    current_delay *= 2
                    continue
                else:
                    return FetchOutcome.TRANSIENT_FAILURE, None
            else:
                return FetchOutcome.INVALID_RESPONSE, None
        except requests.RequestException:
            if attempt < max_retries:
                time.sleep(current_delay)
                current_delay *= 2
                continue
            else:
                return FetchOutcome.TRANSIENT_FAILURE, None

    return FetchOutcome.TRANSIENT_FAILURE, None


def fetch_citing_works_pagination(
    work_id: str,
    citing_limit: int,
    api_params: dict[str, str],
    delay: float,
) -> list[dict[str, Any]]:
    """Fetch citing works using cursor pagination up to citing_limit."""
    results = []
    cursor = "*"
    short_work_id = work_id.rsplit("/", 1)[-1]
    
    while True:
        if citing_limit > 0 and len(results) >= citing_limit:
            break
            
        per_page = 200
        if citing_limit > 0:
            per_page = min(200, citing_limit - len(results))
            
        params = {
            **api_params,
            "filter": f"cites:{short_work_id}",
            "per-page": str(per_page),
            "cursor": cursor,
        }
        
        url = "https://api.openalex.org/works"
        try:
            response = execute_openalex_request(url, params)
            if response.status_code != 200:
                print(f"[WARN] Citing works fetch status {response.status_code} for {work_id}", flush=True)
                break
            payload = response.json()
            batch_results = payload.get("results", [])
            if not batch_results:
                break
            results.extend(batch_results)
            next_cursor = payload.get("meta", {}).get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            time.sleep(delay)
        except Exception as e:
            print(f"[WARN] Citing works fetch failed for {work_id}: {e}", flush=True)
            break
            
    if citing_limit > 0:
        results = results[:citing_limit]
    return results


def validate_checkpoint_config(checkpoint_config: dict, current_config: dict) -> list[str]:
    mismatches = []
    # These parameters must match exactly
    for key in ["journal_code", "max_depth", "citing_limit", "reference_limit", "seed_limit"]:
        if checkpoint_config.get(key) != current_config.get(key):
            mismatches.append(f"{key}: checkpoint={checkpoint_config.get(key)}, current={current_config.get(key)}")
            
    # max_works is allowed to increase, but decreasing it is a mismatch
    cp_max = checkpoint_config.get("max_works", 0)
    curr_max = current_config.get("max_works", 0)
    old_val = float('inf') if cp_max <= 0 else cp_max
    new_val = float('inf') if curr_max <= 0 else curr_max
    if new_val < old_val:
        mismatches.append(f"max_works: decreased limit from {cp_max} to {curr_max}")
        
    return mismatches


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recursive Research Graph Crawler (Option 10)")
    parser.add_argument("--journal-code", type=str, required=True, help="Target journal code in registry")
    parser.add_argument("--registry", type=Path, default=REPO_ROOT / "data" / "vietnam_journals" / "vn_journals_registry.json")
    parser.add_argument("--seed-limit", type=int, default=0, help="Limit number of starting seed articles (0 or -1 for unlimited)")
    parser.add_argument("--max-depth", type=int, default=2, help="Max recursive traversal depth (0 = seeds only, -1 for unlimited)")
    parser.add_argument("--max-works", type=int, default=0, help="Overall limit of works processed (0 or -1 for unlimited)")
    parser.add_argument("--citing-limit", type=int, default=0, help="Citing works limit (0 for unlimited)")
    parser.add_argument("--reference-limit", type=int, default=0, help="Reference works limit (0 for unlimited)")
    parser.add_argument("--batch-size", type=int, default=20, help="DB commit batch size")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between API requests")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries for transient HTTP errors")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint file without prompting")
    parser.add_argument("--ignore-checkpoint-config", action="store_true", help="Force resume even if checkpoint configuration mismatches")
    parser.add_argument("--dry-run", action="store_true", help="Calculate potential size expansion and verify parameters without running crawl")
    return parser


def run_crawl(engine, args, seed_articles=None) -> dict[str, int]:
    # Setup Checkpoint Path
    scratch_dir = REPO_ROOT / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = scratch_dir / f"recursive_graph_{args.journal_code}_checkpoint.json"

    # State variables
    queue = []  # list of [openalex_id, doi, depth, source_relation_info]
    visited = set()
    failed = set()
    stats = {
        "seeds_found": 0,
        "processed_works": 0,
        "edges_discovered": 0,
        "works_fetched": 0,
        "articles_upserted": 0,
        "edges_citing_inserted": 0,
        "edges_reference_inserted": 0,
        "transient_failures": 0,
        "not_found": 0,
        "invalid_response": 0,
    }

    current_config = {
        "journal_code": args.journal_code,
        "max_depth": args.max_depth,
        "max_works": args.max_works,
        "seed_limit": args.seed_limit,
        "citing_limit": args.citing_limit,
        "reference_limit": args.reference_limit,
    }

    # Handle checkpoint loading
    loaded_checkpoint = False
    if not args.dry_run and checkpoint_file.exists():
        resume_choice = False
        if getattr(args, "resume", False):
            resume_choice = True
        else:
            try:
                confirm = input(f"Found checkpoint file '{checkpoint_file.name}'. Resume crawl? (y/N): ").strip().lower()
                if confirm == 'y':
                    resume_choice = True
            except Exception:
                pass

        if resume_choice:
            try:
                with checkpoint_file.open(encoding="utf-8") as f:
                    cp_data = json.load(f)
                
                # Validate config
                mismatches = validate_checkpoint_config(cp_data.get("config", {}), current_config)
                if mismatches:
                    print("[WARNING] Checkpoint configuration mismatches found:")
                    for m in mismatches:
                        print(f"  - {m}")
                    
                    override = False
                    if getattr(args, "ignore_checkpoint_config", False):
                        print("[INFO] Explicit override --ignore-checkpoint-config is active. Resuming crawl...", flush=True)
                        override = True
                    else:
                        if not getattr(args, "resume", False):
                            try:
                                confirm_override = input("Proceed with checkpoint resume despite configuration mismatches? (y/N): ").strip().lower()
                                if confirm_override == 'y':
                                    override = True
                            except Exception:
                                pass
                    
                    if not override:
                        print("[ERROR] Checkpoint configuration mismatch detected and no explicit override provided. Rejecting resume.", flush=True)
                        sys.exit(1)
                
                if resume_choice:
                    queue = cp_data.get("queue", [])
                    visited = set(cp_data.get("visited", []))
                    failed = set(cp_data.get("failed", []))
                    stats.update(cp_data.get("stats", {}))
                    loaded_checkpoint = True
                    print(f"Resumed crawler state: queue={len(queue)}, visited={len(visited)}, failed={len(failed)}", flush=True)
            except SystemExit:
                sys.exit(1)
            except Exception as e:
                print(f"[ERROR] Failed to load checkpoint: {e}. Starting fresh...", flush=True)

    if not loaded_checkpoint:
        if seed_articles is None:
            seed_articles = []
        stats["seeds_found"] = len(seed_articles)
        print("Initializing seed queue from resolved database seeds...", flush=True)
        api_params = fetch_openalex_params()
        for idx, art in enumerate(seed_articles, 1):
            art_id = art.get("article_id")
            oa_id = normalize_work_id(art.get("openalex_id"))
            doi = normalize_doi(art.get("doi"))
            
            # If missing OpenAlex ID but has DOI, resolve it first
            if not oa_id and doi:
                print(f"  [{idx}/{len(seed_articles)}] Resolving OpenAlex ID for seed DOI: {doi}", flush=True)
                outcome, payload = fetch_work_with_outcome(doi, True, args.max_retries, args.delay, api_params)
                if outcome == FetchOutcome.FOUND and payload:
                    oa_id = normalize_work_id(payload.get("id"))
                else:
                    print(f"  [WARN] Failed to resolve OpenAlex ID for DOI {doi}. Outcome: {outcome.name}", flush=True)

            if oa_id or doi:
                queue.append([oa_id, doi, 0, None])
            time.sleep(args.delay)

    # In-memory caches
    author_cache = {}
    keyword_cache = {}
    topic_cache = {}
    enrichment_caches = {
        "author_cache": author_cache,
        "keyword_cache": keyword_cache,
        "topic_cache": topic_cache,
        "openalex_work_by_doi": {},
        "openalex_work_by_id": {},
        "related_article_by_work": {},
    }

    # Queue tracking sets
    queued_ids = {item[0] for item in queue if item[0]}
    queued_dois = {item[1] for item in queue if item[1]}

    print(f"\nStarting crawler loop. Pending queue size: {len(queue)}", flush=True)
    api_params = fetch_openalex_params()
    
    works_processed_since_checkpoint = 0
    seeds_processed_count = 0

    while queue:
        # Check max works limit
        if args.max_works > 0 and stats["processed_works"] >= args.max_works:
            print(f"[INFO] Reached overall work processing limit of {args.max_works}. Stopping.", flush=True)
            if not args.dry_run:
                checkpoint_data = {
                    "config": current_config,
                    "queue": queue,
                    "visited": list(visited),
                    "failed": list(failed),
                    "stats": stats,
                }
                try:
                    temp_cp = checkpoint_file.with_suffix(".tmp")
                    with temp_cp.open("w", encoding="utf-8") as f:
                        json.dump(checkpoint_data, f, indent=2, ensure_ascii=False)
                    if temp_cp.exists():
                        if checkpoint_file.exists():
                            checkpoint_file.unlink()
                        temp_cp.rename(checkpoint_file)
                    print(f"[CHECKPOINT] Saved final state successfully to {checkpoint_file.name}", flush=True)
                except Exception as e:
                    print(f"[WARN] Failed to write checkpoint on limit: {e}", flush=True)
            break

        current_item = queue.pop(0)
        curr_oa_id, curr_doi, curr_depth, relation_info = current_item
        
        # Remove from enqueued trackers
        if curr_oa_id in queued_ids:
            queued_ids.remove(curr_oa_id)
        if curr_doi in queued_dois:
            queued_dois.remove(curr_doi)

        # Skip if visited/failed
        if curr_oa_id and curr_oa_id in visited:
            continue
        if curr_doi and curr_doi in visited:
            continue
        if curr_oa_id and curr_oa_id in failed:
            continue
        if curr_doi and curr_doi in failed:
            continue

        print(f"\nProcessing work: id={curr_oa_id or 'N/A'}, doi={curr_doi or 'N/A'}, depth={curr_depth}", flush=True)

        # Dry-run analysis logic
        if args.dry_run:
            stats["processed_works"] += 1
            stats["works_fetched"] += 1
            if curr_oa_id:
                visited.add(curr_oa_id)
            if curr_doi:
                visited.add(curr_doi)
                
            # Simulate neighbor counts using standard metadata checks
            identifier = curr_oa_id or curr_doi
            is_doi = not bool(curr_oa_id)
            outcome, work_payload = fetch_work_with_outcome(identifier, is_doi, args.max_retries, args.delay, api_params)
            
            if outcome == FetchOutcome.FOUND and work_payload:
                stats["edges_discovered"] += len(work_payload.get("referenced_works", []) or [])
                stats["edges_discovered"] += 1  # Citing estimate
            continue

        # Fetch work details from OpenAlex
        identifier = curr_oa_id or curr_doi
        is_doi = not bool(curr_oa_id)
        
        outcome, work_payload = fetch_work_with_outcome(identifier, is_doi, args.max_retries, args.delay, api_params)
        time.sleep(args.delay)

        if outcome != FetchOutcome.FOUND or not work_payload:
            if outcome == FetchOutcome.NOT_FOUND:
                stats["not_found"] += 1
            elif outcome == FetchOutcome.TRANSIENT_FAILURE:
                stats["transient_failures"] += 1
            else:
                stats["invalid_response"] += 1
            
            if curr_oa_id:
                failed.add(curr_oa_id)
            if curr_doi:
                failed.add(curr_doi)
            print(f"  [WARN] Skip processing. Fetch failed: {outcome.name}", flush=True)
            continue

        # Successfully fetched!
        stats["processed_works"] += 1
        stats["works_fetched"] += 1
        works_processed_since_checkpoint += 1
        
        canonical_oa_id = normalize_work_id(work_payload.get("id"))
        canonical_doi = normalize_doi(work_payload.get("doi"))
        
        if canonical_oa_id:
            visited.add(canonical_oa_id)
        if canonical_doi:
            visited.add(canonical_doi)

        # 1. Map OpenAlex payload to database Article values
        article_payload = openalex_work_to_article(work_payload)
        
        # 2. Persist to database inside a transaction block
        try:
            with engine.begin() as conn:
                # Map topic
                primary_topic_ids = topic_ids_from_openalex_topic(conn, article_payload.get("openalex", {}).get("primary_topic"), topic_cache)
                primary_topic_id = primary_topic_ids[0] if primary_topic_ids else None
                
                # Upsert article
                article_id = upsert_article(conn, article_payload, None, primary_topic_id)
                stats["articles_upserted"] += 1
                print(f"  Upserted Article id={article_id} for '{article_payload.get('title')[:30]}...'", flush=True)

                # Persist Authors & Institutions
                persist_article_authorship_institutions(
                    conn,
                    article_id,
                    article_payload.get("publication_year"),
                    article_payload.get("authors", []) or [],
                    author_cache,
                )
                
                # Link Authors to Article
                for author_data in article_payload.get("authors", []) or []:
                    if not author_data.get("name"):
                        continue
                    author_id = upsert_author(conn, author_data, author_cache)
                    author_position = author_data.get("author_position")
                    link_author_article(conn, author_id, article_id, author_position)

                # Persist Keywords
                for kw_name in article_payload.get("keywords", []) or []:
                    if not kw_name:
                        continue
                    kw_id = upsert_keyword(conn, kw_name, keyword_cache)
                    link_keyword_article(conn, kw_id, article_id, None)

                # Persist Topics
                linked_topic_ids = set(primary_topic_ids)
                for topic in article_payload.get("openalex", {}).get("topics", []) or []:
                    for topic_id in topic_ids_from_openalex_topic(conn, topic, topic_cache):
                        linked_topic_ids.add(topic_id)
                for topic_id in linked_topic_ids:
                    link_sub_topic(conn, article_id, topic_id)

                # Cache the article_id mapping
                if canonical_oa_id:
                    enrichment_caches["related_article_by_work"][canonical_oa_id] = {"article_id": article_id, "openalex_work_id": canonical_oa_id}
                if canonical_doi:
                    enrichment_caches["related_article_by_work"][f"doi:{canonical_doi}"] = {"article_id": article_id, "openalex_work_id": canonical_oa_id}

                # Update pending relationship tables to point to this article_id
                update_pending_relationship_ids(conn, article_id, canonical_oa_id, canonical_doi)

                # 3. Discover neighbors (Citing, Reference, Related)
                should_enqueue = (args.max_depth < 0 or curr_depth < args.max_depth)

                # A. Discover citing works
                citing_works = fetch_citing_works_pagination(canonical_oa_id, args.citing_limit, api_params, args.delay)
                print(f"  Discovered {len(citing_works)} citing works.", flush=True)
                for citing_work in citing_works:
                    citing_work_oa_id = normalize_work_id(citing_work.get("id"))
                    citing_work_doi = normalize_doi(citing_work.get("doi"))
                    
                    # Resolve citing_article_id if it exists in DB
                    citing_art_id = get_existing_article_id(conn, citing_work_oa_id, citing_work_doi)
                    
                    # Edge logging
                    upsert_article_citing_work(conn, article_id, citing_work, citing_art_id)
                    stats["edges_citing_inserted"] += 1
                    
                    # Enqueue if permitted
                    if should_enqueue and (citing_work_oa_id or citing_work_doi):
                        is_vis = (citing_work_oa_id in visited) or (citing_work_doi in visited)
                        is_fail = (citing_work_oa_id in failed) or (citing_work_doi in failed)
                        is_q = (citing_work_oa_id in queued_ids) or (citing_work_doi in queued_dois)
                        if not is_vis and not is_fail and not is_q:
                            queue.append([citing_work_oa_id, citing_work_doi, curr_depth + 1, {
                                "parent_openalex_id": canonical_oa_id,
                                "relation_type": "citing"
                            }])
                            if citing_work_oa_id:
                                queued_ids.add(citing_work_oa_id)
                            if citing_work_doi:
                                queued_dois.add(citing_work_doi)

                # B. Discover references
                ref_ids = work_payload.get("referenced_works", []) or []
                if args.reference_limit > 0:
                    ref_ids = ref_ids[:args.reference_limit]
                print(f"  Discovered {len(ref_ids)} references.", flush=True)
                for ref_idx, ref_id in enumerate(ref_ids):
                    ref_oa_id = normalize_work_id(ref_id)
                    ref_art_id = get_existing_article_id(conn, ref_oa_id, None)
                    
                    # Edge logging
                    payload_ref = {"work_id": ref_oa_id, "id": ref_oa_id}
                    upsert_article_reference(conn, article_id, payload_ref, ref_idx, ref_art_id)
                    stats["edges_reference_inserted"] += 1
                    
                    # Enqueue if permitted
                    if should_enqueue and ref_oa_id:
                        is_vis = (ref_oa_id in visited)
                        is_fail = (ref_oa_id in failed)
                        is_q = (ref_oa_id in queued_ids)
                        if not is_vis and not is_fail and not is_q:
                            queue.append([ref_oa_id, None, curr_depth + 1, {
                                "parent_openalex_id": canonical_oa_id,
                                "relation_type": "reference",
                                "index": ref_idx
                            }])
                            queued_ids.add(ref_oa_id)
        except ValueError as e:
            if curr_oa_id:
                failed.add(curr_oa_id)
            if curr_doi:
                failed.add(curr_doi)
            print(f"  [WARN] Skip processing work due to identity conflict: {e}", flush=True)
            stats["processed_works"] = max(0, stats["processed_works"] - 1)
            works_processed_since_checkpoint = max(0, works_processed_since_checkpoint - 1)
            continue

        # Track seeds count
        if curr_depth == 0:
            seeds_processed_count += 1

        # Checkpoint saving trigger
        should_save_checkpoint = False
        if curr_depth == 0:
            should_save_checkpoint = True
        elif works_processed_since_checkpoint >= args.batch_size:
            should_save_checkpoint = True
            
        if should_save_checkpoint:
            checkpoint_data = {
                "config": current_config,
                "queue": queue,
                "visited": list(visited),
                "failed": list(failed),
                "stats": stats,
            }
            try:
                temp_cp = checkpoint_file.with_suffix(".tmp")
                with temp_cp.open("w", encoding="utf-8") as f:
                    json.dump(checkpoint_data, f, indent=2, ensure_ascii=False)
                if temp_cp.exists():
                    if checkpoint_file.exists():
                        checkpoint_file.unlink()
                    temp_cp.rename(checkpoint_file)
                print(f"[CHECKPOINT] Saved state successfully to {checkpoint_file.name}", flush=True)
                works_processed_since_checkpoint = 0
            except Exception as e:
                print(f"[WARN] Failed to write checkpoint: {e}", flush=True)

    # Crawl completed successfully! Remove checkpoint
    # Crawl completed successfully! Remove checkpoint if queue is empty
    if not args.dry_run and checkpoint_file.exists():
        if len(queue) == 0:
            try:
                checkpoint_file.unlink()
                print("[INFO] Crawl completed. Checkpoint file removed.", flush=True)
            except Exception as e:
                print(f"[WARN] Failed to remove checkpoint file: {e}", flush=True)
        else:
            print(f"[INFO] Halted with {len(queue)} pending works in queue. Preserving checkpoint for resume.", flush=True)

    return stats


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()

    # Load registry
    if not args.registry.exists():
        print(f"[ERROR] Registry file not found at {args.registry}", flush=True)
        sys.exit(1)
    
    with args.registry.open(encoding="utf-8") as f:
        registry = json.load(f)
    
    if args.journal_code not in registry:
        print(f"[ERROR] Journal code '{args.journal_code}' not found in registry", flush=True)
        sys.exit(1)
        
    journal_entry = registry[args.journal_code]
    base_url = journal_entry.get("base_url")
    display_name = journal_entry.get("name_en") or journal_entry.get("name_vi")
    
    load_env()
    supabase_url = get_supabase_url()
    engine = create_engine(supabase_url)

    # Resolve seeds from database
    print(f"Connecting to database to resolve seeds for '{display_name}'...", flush=True)
    with engine.connect() as conn:
        # Try to resolve by name first
        journal_row = conn.execute(
            text('SELECT "journal_id" FROM "Journal" WHERE lower("display_name") = lower(:display_name) ORDER BY "journal_id" LIMIT 1'),
            {"display_name": display_name},
        ).fetchone()
        
        # Fallback to source_id if name not found and base_url is not a placeholder
        is_placeholder = base_url and ("about.lens.org" in base_url.lower() or "example.com" in base_url.lower())
        if not journal_row and base_url and not is_placeholder:
            journal_row = conn.execute(
                text('SELECT "journal_id" FROM "Journal" WHERE "source_id" = :base_url ORDER BY "journal_id" LIMIT 1'),
                {"base_url": base_url},
            ).fetchone()
        
        if not journal_row:
            print(f"[ERROR] Journal '{display_name}' not found in database.", flush=True)
            sys.exit(1)
            
        journal_id = int(journal_row[0])
        
        db_articles = conn.execute(
            text(
                'SELECT a."article_id", a."openalex_id", a."doi", a."title" '
                'FROM "Article" a '
                'JOIN "Issue" i ON a."issue_id" = i."issue_id" '
                'JOIN "Volume" v ON i."volume_id" = v."volume_id" '
                'WHERE v."journal_id" = :journal_id '
                'ORDER BY a."article_id" ASC'
            ),
            {"journal_id": journal_id},
        ).mappings().all()

    total_db_articles = len(db_articles)
    print(f"Found {total_db_articles} articles in database for this journal.", flush=True)
    
    if total_db_articles == 0:
        print("[ERROR] Cannot crawl: no seed articles exist in database.", flush=True)
        sys.exit(1)

    seed_limit = args.seed_limit
    if seed_limit > 0:
        seed_articles = db_articles[:seed_limit]
    else:
        seed_articles = db_articles

    seed_count = len(seed_articles)
    print(f"Resolved {seed_count} seed articles for crawling.", flush=True)

    # Dry-Run mode display logic (delegates loop execution to run_crawl but handles output here)
    if args.dry_run:
        print("\n" + "="*60)
        print("DRY-RUN EXPANSION ANALYSIS:")
        print("="*60)
        print(f"Seeds loaded:       {seed_count}")
        print(f"Max depth:          {args.max_depth if args.max_depth >= 0 else 'unlimited'}")
        print(f"Citing limit:       {args.citing_limit if args.citing_limit > 0 else 'unlimited'}")
        print(f"Reference limit:    {args.reference_limit if args.reference_limit > 0 else 'unlimited'}")
        
        # Calculate estimate
        c_val = args.citing_limit if args.citing_limit > 0 else 50
        r_val = args.reference_limit if args.reference_limit > 0 else 40
        expansion_factor = c_val + r_val
        
        print("\nExpansion Estimates (using standard estimations if unlimited):")
        total_estimate = seed_count
        current_layer = seed_count
        
        depth_limit = args.max_depth if args.max_depth >= 0 else 3
        for d in range(1, depth_limit + 1):
            current_layer = current_layer * expansion_factor
            total_estimate += current_layer
            print(f"  Depth {d} estimate: +{current_layer:,} works (cumulative: {total_estimate:,})")
            
        print("\n[WARNING] Recursive crawls grow exponentially! Ensure limits are configured correctly.")
        print("DRY-RUN analysis starting query simulation...")
        
    stats = run_crawl(engine, args, seed_articles)

    if args.dry_run:
        print(f"\nDRY-RUN simulation finished. Simulated fetching {stats['works_fetched']} works, discovered {stats['edges_discovered']} potential graph edges.")
        print("DRY-RUN completed. No changes made.")
        print("="*60 + "\n")
        return

    print("\n" + "="*50)
    print("RECURSIVE GRAPH CRAWL STATS:")
    print("="*50)
    print(f"Works Fetched:        {stats['works_fetched']}")
    print(f"Articles Upserted:    {stats['articles_upserted']}")
    print(f"Edges Citing:         {stats['edges_citing_inserted']}")
    print(f"Edges Reference:      {stats['edges_reference_inserted']}")
    print(f"Not Found (404):      {stats['not_found']}")
    print(f"Transient Failures:   {stats['transient_failures']}")
    print(f"Invalid Responses:    {stats['invalid_response']}")
    print("="*50 + "\n")


if __name__ == "__main__":
    main()
