from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    get_vn_country_zone,
    load_env,
    normalize_issn,
    sync_identity_sequence,
    upsert_publisher,
)
from tools.vn_journals.paper_vn_affiliations import (
    persist_article_authorship_institutions,
    upsert_author_safe,
)
from tools.vn_journals.paper_vn_article_metadata import (
    build_article_payload,
    citation_history_json,
    merge_article_values,
    normalize_doi as normalize_article_doi,
    normalize_work_id,
    safe_title_year_candidate_ids,
)


def short_openalex_id(value: Any) -> str | None:
    normalized = normalize_work_id(value)
    return normalized.rsplit("/", 1)[-1] if normalized else None


def doi_lookup_sql() -> str:
    normalized_expr = (
        'trim(replace(replace(replace(lower(trim("doi")), '
        "'https://dx.doi.org/', ''), 'https://doi.org/', ''), 'doi:', ''))"
    )
    return (
        'lower(trim("doi")) = :doi OR '
        'lower(trim("doi")) = :doi_prefix OR '
        'lower(trim("doi")) = :doi_url OR '
        'lower(trim("doi")) = :dx_doi_url OR '
        f"{normalized_expr} = :doi"
    )


def doi_lookup_params(doi: str) -> dict[str, str]:
    return {
        "doi": doi,
        "doi_prefix": f"doi:{doi}",
        "doi_url": f"https://doi.org/{doi}",
        "dx_doi_url": f"https://dx.doi.org/{doi}",
    }


def load_package(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unsupported VN journal JSON structure: {path}")


def upsert_journal(conn, journal: dict[str, Any], publisher_id: int | None, country_id: int | None) -> int:
    display_name = journal.get("name_en") or journal.get("name_vi")
    if not display_name:
        raise ValueError("Journal requires name_en or name_vi")

    source_id = journal.get("base_url")
    if source_id and ("about.lens.org" in source_id.lower() or "example.com" in source_id.lower()):
        source_id = None

    payload = {
        "owning_institution": journal.get("owning_institution"),
        "source_id": source_id,
        "publisher_id": publisher_id,
        "country": country_id,
        "display_name": display_name,
        "type": (journal.get("type") or "journal").lower(),
        "coverage": journal.get("coverage"),
        "issn": normalize_issn(journal),
        "is_deleted": False,
    }

    # Match by display_name first (case-insensitive)
    existing = conn.execute(
        text(
            'SELECT "journal_id" FROM "Journal" '
            'WHERE lower("display_name") = lower(:display_name) '
            'ORDER BY "journal_id" LIMIT 1'
        ),
        {"display_name": display_name},
    ).fetchone()

    # Match by source_id next if name wasn't found and we have a valid source_id
    if not existing and source_id:
        existing = conn.execute(
            text(
                'SELECT "journal_id" FROM "Journal" '
                'WHERE "source_id" = :source_id '
                'ORDER BY "journal_id" LIMIT 1'
            ),
            {"source_id": source_id},
        ).fetchone()

    if existing:
        journal_id = int(existing[0])
        conn.execute(
            text(
                'UPDATE "Journal" SET '
                '"owning_institution" = :owning_institution, '
                '"source_id" = :source_id, '
                '"publisher_id" = :publisher_id, '
                '"country" = :country, '
                '"display_name" = :display_name, '
                '"type" = :type, '
                '"coverage" = :coverage, '
                '"issn" = :issn, '
                '"is_deleted" = :is_deleted '
                'WHERE "journal_id" = :journal_id'
            ),
            {**payload, "journal_id": journal_id},
        )
        return journal_id

    sync_identity_sequence(conn, "Journal", "journal_id")
    return int(
        conn.execute(
            text(
                'INSERT INTO "Journal" '
                '("owning_institution", "source_id", "publisher_id", "country", "display_name", "type", "coverage", "issn", "is_deleted") '
                'VALUES (:owning_institution, :source_id, :publisher_id, :country, :display_name, :type, :coverage, :issn, :is_deleted) '
                'RETURNING "journal_id"'
            ),
            payload,
        ).scalar_one()
    )


def upsert_volume(conn, journal_id: int, volume: str | None, year: int | None, cache: dict) -> int | None:
    if not volume and not year:
        return None

    vol_num = None
    if volume:
        try:
            vol_num = int(volume)
        except ValueError:
            pass

    key = (journal_id, vol_num, year)
    if key in cache:
        return cache[key]

    if vol_num is not None:
        row = conn.execute(
            text(
                'SELECT "volume_id" FROM "Volume" '
                'WHERE "journal_id" = :journal_id AND "volume_number" = :volume_number LIMIT 1'
            ),
            {"journal_id": journal_id, "volume_number": vol_num},
        ).fetchone()
    else:
        row = conn.execute(
            text(
                'SELECT "volume_id" FROM "Volume" '
                'WHERE "journal_id" = :journal_id AND "volume_number" IS NULL AND "publication_year" = :year LIMIT 1'
            ),
            {"journal_id": journal_id, "year": year},
        ).fetchone()

    if row:
        val = int(row[0])
        cache[key] = val
        return val

    sync_identity_sequence(conn, "Volume", "volume_id")
    val = int(
        conn.execute(
            text(
                'INSERT INTO "Volume" ("journal_id", "volume_number", "publication_year") '
                'VALUES (:journal_id, :volume_number, :publication_year) RETURNING "volume_id"'
            ),
            {"journal_id": journal_id, "volume_number": vol_num, "publication_year": year},
        ).scalar_one()
    )
    cache[key] = val
    return val


def upsert_issue(conn, volume_id: int | None, issue: str | None, year: int | None, cache: dict) -> int | None:
    if not volume_id and not issue:
        return None

    key = (volume_id, issue, year)
    if key in cache:
        return cache[key]

    row = conn.execute(
        text('SELECT "issue_id" FROM "Issue" WHERE "volume_id" = :volume_id AND "issue_number" = :issue LIMIT 1'),
        {"volume_id": volume_id, "issue": issue},
    ).fetchone()

    if row:
        val = int(row[0])
        cache[key] = val
        return val

    sync_identity_sequence(conn, "Issue", "issue_id")
    val = int(
        conn.execute(
            text(
                'INSERT INTO "Issue" ("volume_id", "issue_number", "publication_year") '
                'VALUES (:volume_id, :issue_number, :publication_year) RETURNING "issue_id"'
            ),
            {"volume_id": volume_id, "issue_number": issue, "publication_year": year},
        ).scalar_one()
    )
    cache[key] = val
    return val



def split_topic_display_name(value: Any) -> list[str]:
    """Split OpenAlex topic labels into smaller DB topics.

    Example: "Advanced Differential Equations and Dynamical Systems"
    becomes ["Advanced Differential Equations", "Dynamical Systems"].
    """
    if not value:
        return []
    text_value = str(value).strip()
    if not text_value:
        return []
    parts = re.split(r"\s*,\s*|\s+and\s+", text_value, flags=re.IGNORECASE)
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = re.sub(r"\s+", " ", part).strip(" .;:-")
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(name)
    return cleaned


def get_or_create_subject_info(conn, field_name: str | None, subfield_name: str | None) -> tuple[int | None, int | None]:
    subject_area_id = None
    subject_category_id = None

    if field_name:
        field_names = split_topic_display_name(field_name)
        for name in field_names:
            row = conn.execute(
                text('SELECT "subject_area_id" FROM "Subject_Area" WHERE lower("display_name") = lower(:name) LIMIT 1'),
                {"name": name},
            ).fetchone()
            if row:
                area_id = int(row[0])
            else:
                sync_identity_sequence(conn, "Subject_Area", "subject_area_id")
                area_id = int(conn.execute(
                    text('INSERT INTO "Subject_Area" ("display_name") VALUES (:name) RETURNING "subject_area_id"'),
                    {"name": name},
                ).scalar_one())
            if subject_area_id is None:
                subject_area_id = area_id

    # Split real combined subject categories such as "Geometry and Topology"
    # into two Subject_Category rows: "Geometry" and "Topology".
    # Topic.subject_category_id can store only one primary category, so the
    # first created/resolved category is returned as the primary link.
    subfield_names = split_topic_display_name(subfield_name)
    for category_name in subfield_names:
        row = conn.execute(
            text('SELECT "subject_category_id" FROM "Subject_Category" WHERE lower("display_name") = lower(:name) LIMIT 1'),
            {"name": category_name},
        ).fetchone()
        if row:
            category_id = int(row[0])
        else:
            sync_identity_sequence(conn, "Subject_Category", "subject_category_id")
            category_id = int(conn.execute(
                text('INSERT INTO "Subject_Category" ("subject_area_id", "display_name") VALUES (:area_id, :name) RETURNING "subject_category_id"'),
                {"area_id": subject_area_id, "name": category_name},
            ).scalar_one())
        if subject_category_id is None:
            subject_category_id = category_id

    return subject_area_id, subject_category_id


def upsert_topic(conn, display_name: str, score: float | None, field_name: str | None, subfield_name: str | None, cache: dict) -> int:
    name = display_name.strip()
    key = name.lower()
    if key in cache:
        return cache[key]

    row = conn.execute(
        text('SELECT "topic_id" FROM "Topic" WHERE lower("display_name") = lower(:name) LIMIT 1'),
        {"name": name},
    ).fetchone()
    if row:
        topic_id = int(row[0])
        cache[key] = topic_id
        return topic_id

    subject_area_id, subject_category_id = get_or_create_subject_info(conn, field_name, subfield_name)
    sync_identity_sequence(conn, "Topic", "topic_id")
    topic_id = int(conn.execute(
        text(
            'INSERT INTO "Topic" ("display_name", "score", "subject_area_id", "subject_category_id") '
            'VALUES (:name, :score, :area_id, :cat_id) RETURNING "topic_id"'
        ),
        {"name": name, "score": score, "area_id": subject_area_id, "cat_id": subject_category_id},
    ).scalar_one())
    cache[key] = topic_id
    return topic_id


def topic_ids_from_openalex_topic(conn, topic: dict[str, Any] | None, cache: dict) -> list[int]:
    if not isinstance(topic, dict):
        return []
    names = split_topic_display_name(topic.get("display_name"))
    if not names:
        return []
    score = topic.get("score")
    field_name = (topic.get("field") or {}).get("display_name")
    subfield_name = (topic.get("subfield") or {}).get("display_name")
    return [upsert_topic(conn, name, score, field_name, subfield_name, cache) for name in names]


def link_sub_topic(conn, article_id: int, topic_id: int) -> None:
    row = conn.execute(
        text('SELECT 1 FROM "Sub_Topic" WHERE "article_id" = :article_id AND "topic_id" = :topic_id'),
        {"article_id": article_id, "topic_id": topic_id},
    ).fetchone()
    if not row:
        conn.execute(
            text('INSERT INTO "Sub_Topic" ("article_id", "topic_id") VALUES (:article_id, :topic_id)'),
            {"article_id": article_id, "topic_id": topic_id},
        )


def normalize_doi(value: Any) -> str | None:
    return normalize_article_doi(value)


def reconstruct_abstract(inverted_index: Any) -> str | None:
    """Convert OpenAlex abstract_inverted_index into plain text when available."""
    if not isinstance(inverted_index, dict):
        return None
    positions: dict[int, str] = {}
    for word, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            try:
                positions[int(index)] = str(word)
            except (TypeError, ValueError):
                continue
    if not positions:
        return None
    return " ".join(positions[index] for index in sorted(positions))


def primary_location(work: dict[str, Any]) -> dict[str, Any]:
    location = work.get("primary_location") or work.get("best_oa_location") or {}
    return location if isinstance(location, dict) else {}


def openalex_work_to_article(work: dict[str, Any]) -> dict[str, Any]:
    location = primary_location(work)
    best_oa_location = work.get("best_oa_location") if isinstance(work.get("best_oa_location"), dict) else {}
    source = location.get("source") or {}
    authors: list[dict[str, Any]] = []
    if work.get("authorships"):
        for authorship in work.get("authorships", []) or []:
            author = authorship.get("author") or {}
            if not author.get("display_name"):
                continue
            institutions = authorship.get("institutions") or []
            authors.append(
                {
                    "name": author.get("display_name"),
                    "orcid": author.get("orcid"),
                    "openalex_author_id": author.get("id"),
                    "author_position": authorship.get("author_position"),
                    "institutions": institutions,
                    "affiliation": institutions[0].get("display_name") if institutions else None,
                }
            )
    else:
        for author in work.get("authors", []) or []:
            if isinstance(author, dict) and author.get("name"):
                authors.append(dict(author))

    return {
        "title": work.get("title") or work.get("display_name"),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "publication_year": work.get("publication_year"),
        "doi": normalize_doi(work.get("doi")),
        "landing_url": location.get("landing_page_url") or work.get("landing_url"),
        "pdf_url": location.get("pdf_url") or work.get("pdf_url"),
        "openalex": {
            "work_id": normalize_work_id(work.get("id") or work.get("work_id")),
            "cited_by_count": work.get("cited_by_count"),
            "referenced_works_count": work.get("referenced_works_count"),
            "counts_by_year": work.get("counts_by_year") or [],
            "open_access": work.get("open_access") or {},
            "best_oa_location": best_oa_location,
            "keywords": work.get("keywords") or [],
            "topics": work.get("topics") or [],
            "primary_topic": work.get("primary_topic"),
            "landing_url": best_oa_location.get("landing_page_url") or location.get("landing_page_url"),
            "pdf_url": best_oa_location.get("pdf_url") or location.get("pdf_url"),
        },
        "authors": authors,
        "keywords": [kw.get("display_name") for kw in work.get("keywords", []) or [] if kw.get("display_name")],
        "source_url": source.get("id"),
        "raw": {"openalex_source": work},
    }


def openalex_params() -> dict[str, str]:
    params: dict[str, str] = {}
    mailto = os.getenv("OPENALEX_EMAIL")
    api_key = os.getenv("OPENALEX_API_KEY")
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    return params


def fetch_openalex_work_by_doi(doi: str, cache: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    if normalized in cache:
        return cache[normalized]

    encoded_doi = requests.utils.quote(normalized, safe="")
    url = f"https://api.openalex.org/works/https://doi.org/{encoded_doi}"
    try:
        response = requests.get(url, params=openalex_params(), timeout=30)
        if response.status_code == 404:
            cache[normalized] = None
            return None
        response.raise_for_status()
        payload = response.json()
        cache[normalized] = payload if isinstance(payload, dict) else None
        return cache[normalized]
    except requests.RequestException as exc:
        print(f"[WARN] OpenAlex DOI fetch failed for {normalized}: {exc}", flush=True)
        cache[normalized] = None
        return None


def fetch_openalex_work_by_id(work_id: str, cache: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    normalized = normalize_work_id(work_id)
    if not normalized:
        return None
    if normalized in cache:
        return cache[normalized]

    short_id = normalized.rsplit("/", 1)[-1]
    url = f"https://api.openalex.org/works/{short_id}"
    try:
        response = requests.get(url, params=openalex_params(), timeout=30)
        if response.status_code == 404:
            cache[normalized] = None
            return None
        response.raise_for_status()
        payload = response.json()
        cache[normalized] = payload if isinstance(payload, dict) else None
        return cache[normalized]
    except requests.RequestException as exc:
        print(f"[WARN] OpenAlex work fetch failed for {normalized}: {exc}", flush=True)
        cache[normalized] = None
        return None


def enrich_related_work_article(
    conn,
    work: dict[str, Any],
    caches: dict[str, Any],
    stats: dict[str, int],
) -> int | None:
    doi = normalize_doi(work.get("doi"))
    openalex_work_id = normalize_work_id(work.get("work_id") or work.get("id") or work.get("openalex_work_id"))
    cache_key = openalex_work_id or (f"doi:{doi}" if doi else None)
    if not cache_key:
        return None

    stats["related_works_seen"] = stats.get("related_works_seen", 0) + 1
    if cache_key in caches["related_article_by_work"]:
        cached = caches["related_article_by_work"][cache_key]
        if isinstance(cached, dict):
            resolved_work_id = cached.get("openalex_work_id")
            if resolved_work_id:
                work["openalex_work_id"] = resolved_work_id
                work.setdefault("id", resolved_work_id)
            return cached.get("article_id")
        return cached

    fetched = None
    if openalex_work_id:
        fetched = fetch_openalex_work_by_id(openalex_work_id, caches["openalex_work_by_id"])
    if not fetched and doi:
        stats["related_dois_seen"] = stats.get("related_dois_seen", 0) + 1
        fetched = fetch_openalex_work_by_doi(doi, caches["openalex_work_by_doi"])

    if fetched:
        stats["related_works_fetched"] = stats.get("related_works_fetched", 0) + 1
        article_payload = openalex_work_to_article(fetched)
        resolved_work_id = article_payload.get("openalex", {}).get("work_id")
        if resolved_work_id:
            work["openalex_work_id"] = resolved_work_id
            work.setdefault("id", resolved_work_id)
    elif work.get("title") and (work.get("authorships") or work.get("authors")):
        article_payload = openalex_work_to_article(work)
    else:
        stats["related_works_missing"] = stats.get("related_works_missing", 0) + 1
        if doi:
            stats["related_dois_missing"] = stats.get("related_dois_missing", 0) + 1
        caches["related_article_by_work"][cache_key] = None
        return None

    if not article_payload.get("title"):
        stats["related_works_missing"] = stats.get("related_works_missing", 0) + 1
        caches["related_article_by_work"][cache_key] = None
        return None

    primary_topic_ids = topic_ids_from_openalex_topic(conn, article_payload.get("openalex", {}).get("primary_topic"), caches["topic_cache"])
    try:
        article_id = upsert_article(conn, article_payload, None, primary_topic_ids[0] if primary_topic_ids else None)
    except ValueError as exc:
        stats["related_identity_conflicts"] = stats.get("related_identity_conflicts", 0) + 1
        stats["related_works_missing"] = stats.get("related_works_missing", 0) + 1
        if doi:
            stats["related_dois_missing"] = stats.get("related_dois_missing", 0) + 1
        print(f"[WARN] Related work identity conflict skipped: {exc}", flush=True)
        caches["related_article_by_work"][cache_key] = None
        return None
    affil_stats = persist_article_authorship_institutions(
        conn,
        article_id,
        article_payload.get("publication_year"),
        article_payload.get("authors", []) or [],
        caches["author_cache"],
    )
    stats["institutions_found"] = stats.get("institutions_found", 0) + affil_stats.institutions_found
    stats["institution_links_inserted"] = stats.get("institution_links_inserted", 0) + affil_stats.institution_links_inserted

    for kw_obj in article_payload.get("openalex", {}).get("keywords", []) or []:
        kw_name = kw_obj.get("display_name")
        if not kw_name:
            continue
        kw_id = upsert_keyword(conn, kw_name, caches["keyword_cache"])
        link_keyword_article(conn, kw_id, article_id, kw_obj.get("score"))

    linked_topic_ids: set[int] = set(primary_topic_ids)
    for topic in article_payload.get("openalex", {}).get("topics", []) or []:
        for topic_id in topic_ids_from_openalex_topic(conn, topic, caches["topic_cache"]):
            linked_topic_ids.add(topic_id)
    for topic_id in linked_topic_ids:
        link_sub_topic(conn, article_id, topic_id)

    resolved_work_id = article_payload.get("openalex", {}).get("work_id") or normalize_work_id(
        work.get("openalex_work_id") or work.get("work_id") or work.get("id")
    )
    cached_value = {"article_id": article_id, "openalex_work_id": resolved_work_id}
    caches["related_article_by_work"][cache_key] = cached_value
    if doi:
        caches["related_article_by_work"][f"doi:{doi}"] = cached_value
    if resolved_work_id:
        caches["related_article_by_work"][resolved_work_id] = cached_value
    stats["related_articles_upserted"] = stats.get("related_articles_upserted", 0) + 1
    return article_id


def normalize_relationship_work(work: dict[str, Any]) -> dict[str, Any]:
    source = work.get("source") or {}
    return {
        "openalex_work_id": normalize_work_id(work.get("work_id") or work.get("id") or work.get("openalex_work_id")),
        "doi": normalize_doi(work.get("doi")),
        "title": work.get("title") or work.get("display_name"),
        "publication_year": work.get("publication_year"),
        "source_name": source.get("display_name") or work.get("source_name"),
        "source_url": source.get("id") or work.get("source_url"),
        "landing_url": work.get("landing_url") or work.get("landing_page_url"),
        "pdf_url": work.get("pdf_url"),
        "cited_by_count": work.get("cited_by_count"),
        "type": work.get("type"),
        "authors": json.dumps(work.get("authors") or [], ensure_ascii=False),
        "raw": json.dumps(work, ensure_ascii=False),
    }


def make_reference_key(work: dict[str, Any], index: int) -> str:
    openalex_id = normalize_work_id(work.get("work_id") or work.get("id") or work.get("openalex_work_id"))
    if openalex_id:
        return str(openalex_id)
    semantic_id = work.get("semantic_scholar_id") or work.get("paperId")
    if semantic_id:
        return f"semantic:{semantic_id}"
    doi = normalize_doi(work.get("doi"))
    if doi:
        return f"doi:{doi}"
    raw_key = f"{work.get('title') or ''}|{work.get('publication_year') or ''}|{index}"
    return "hash:" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()


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
            '"citing_article_id" = COALESCE(EXCLUDED."citing_article_id", "Article_Citing_Work"."citing_article_id"), '
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
            '"referenced_article_id" = COALESCE(EXCLUDED."referenced_article_id", "Article_Reference"."referenced_article_id"), '
            '"updated_at" = CURRENT_TIMESTAMP'
        ),
        payload,
    )

def upsert_article(conn, article: dict[str, Any], issue_id: int | None, primary_topic_id: int | None = None, is_vn_journal: bool = False) -> int:
    payload = build_article_payload(article, issue_id, primary_topic_id)
    doi = payload.get("doi")
    title = payload.get("title")
    if not title:
        raise ValueError("Article requires a title")
    openalex_id = payload.get("openalex_id")

    matched_by = None
    doi_ids: set[int] = set()
    openalex_ids: set[int] = set()
    if doi:
        doi_rows = conn.execute(
            text(f'SELECT "article_id", "doi" FROM "Article" WHERE {doi_lookup_sql()}'),
            doi_lookup_params(doi),
        ).fetchall()
        doi_ids = {int(row[0]) for row in doi_rows if normalize_article_doi(row[1]) == doi}

    if openalex_id:
        openalex_rows = conn.execute(
            text('SELECT "article_id" FROM "Article" WHERE "openalex_id" = :openalex_id OR "openalex_id" = :short_openalex_id'),
            {"openalex_id": openalex_id, "short_openalex_id": short_openalex_id(openalex_id)},
        ).fetchall()
        openalex_ids = {int(row[0]) for row in openalex_rows}

    candidate_ids: set[int] = set()
    if doi_ids and openalex_ids:
        candidate_ids = doi_ids & openalex_ids
        if not candidate_ids:
            raise ValueError(f"Article identity conflict: DOI {doi} and OpenAlex ID {openalex_id} match different rows")
        matched_by = "doi_openalex"
    elif doi_ids:
        candidate_ids = doi_ids
        matched_by = "doi"
    elif openalex_ids:
        candidate_ids = openalex_ids
        matched_by = "openalex"

    if len(candidate_ids) > 1:
        raise ValueError(f"Ambiguous Article identity for DOI {doi!r} OpenAlex ID {openalex_id!r}: {sorted(candidate_ids)}")

    if candidate_ids:
        article_id = next(iter(candidate_ids))
        existing = conn.execute(
            text(
                'SELECT "title", "abstract", "publication_year", "doi", "primary_topic", "citation_count", '
                '"reference_count", "openalex_id", "landing_url", "pdf_url", "pages", "is_open_access", '
                '"citing_patents_count", "citations_by_year", "issue_id", "is_vn_journal" '
                'FROM "Article" WHERE "article_id" = :article_id'
            ),
            {"article_id": article_id},
        ).mappings().fetchone()
        existing_values = dict(existing or {})
        existing_doi = normalize_article_doi(existing_values.get("doi"))
        existing_openalex_id = normalize_work_id(existing_values.get("openalex_id"))
        if doi and existing_doi and existing_doi != doi:
            raise ValueError(f"Article DOI conflict for article_id {article_id}: {existing_doi} != {doi}")
        if matched_by in {"doi", "doi_openalex"} and openalex_id and existing_openalex_id and existing_openalex_id != openalex_id:
            raise ValueError(
                f"Article OpenAlex conflict for DOI {doi}: existing {existing_openalex_id} != incoming {openalex_id}"
            )
        if matched_by in {"openalex", "doi_openalex"} and doi and existing_doi and existing_doi != doi:
            raise ValueError(
                f"Article DOI conflict for OpenAlex ID {openalex_id}: existing {existing_doi} != incoming {doi}"
            )
        existing_is_vn = existing_values.get("is_vn_journal", False)
        existing_values["issue_id"] = existing_values.pop("issue_id", None)
        merged = merge_article_values(existing_values, payload)
        merged["citations_by_year"] = citation_history_json(merged.get("citations_by_year"))
        merged["is_vn_journal"] = existing_is_vn or is_vn_journal
        conn.execute(
            text(
                'UPDATE "Article" SET '
                '"issue_id" = :issue_id, "title" = :title, "abstract" = :abstract, "publication_year" = :publication_year, '
                '"doi" = :doi, '
                '"primary_topic" = :primary_topic, "citation_count" = :citation_count, "reference_count" = :reference_count, '
                '"openalex_id" = :openalex_id, "landing_url" = :landing_url, "pdf_url" = :pdf_url, "pages" = :pages, '
                '"is_open_access" = :is_open_access, "citing_patents_count" = :citing_patents_count, '
                '"citations_by_year" = CAST(:citations_by_year AS JSONB), "is_vn_journal" = :is_vn_journal '
                'WHERE "article_id" = :article_id'
            ),
            {**merged, "article_id": article_id},
        )
        return article_id

    fallback_rows = []
    if payload.get("publication_year") is not None:
        fallback_rows = conn.execute(
            text(
                'SELECT "article_id", "doi", "openalex_id" FROM "Article" '
                'WHERE lower(trim("title")) = lower(trim(:title)) AND "publication_year" = :publication_year'
            ),
            {"title": title, "publication_year": payload.get("publication_year")}
        ).fetchall()
    safe_fallback_ids = safe_title_year_candidate_ids(fallback_rows, doi, openalex_id)
    if len(safe_fallback_ids) == 1:
        article_id = safe_fallback_ids[0]
        existing = conn.execute(
            text(
                'SELECT "title", "abstract", "publication_year", "doi", "primary_topic", "citation_count", '
                '"reference_count", "openalex_id", "landing_url", "pdf_url", "pages", "is_open_access", '
                '"citing_patents_count", "citations_by_year", "issue_id", "is_vn_journal" '
                'FROM "Article" WHERE "article_id" = :article_id'
            ),
            {"article_id": article_id},
        ).mappings().fetchone()
        existing_values = dict(existing or {})
        existing_is_vn = existing_values.get("is_vn_journal", False)
        existing_values["issue_id"] = existing_values.pop("issue_id", None)
        merged = merge_article_values(existing_values, payload)
        merged["citations_by_year"] = citation_history_json(merged.get("citations_by_year"))
        merged["is_vn_journal"] = existing_is_vn or is_vn_journal
        conn.execute(
            text(
                'UPDATE "Article" SET '
                '"issue_id" = :issue_id, "abstract" = :abstract, "title" = :title, "publication_year" = :publication_year, '
                '"primary_topic" = :primary_topic, "citation_count" = :citation_count, "reference_count" = :reference_count, '
                '"openalex_id" = :openalex_id, "landing_url" = :landing_url, "pdf_url" = :pdf_url, "pages" = :pages, '
                '"is_open_access" = :is_open_access, "doi" = :doi, '
                '"citing_patents_count" = :citing_patents_count, "citations_by_year" = CAST(:citations_by_year AS JSONB), '
                '"is_vn_journal" = :is_vn_journal '
                'WHERE "article_id" = :article_id'
            ),
            {**merged, "article_id": article_id},
        )
        return article_id

    sync_identity_sequence(conn, "Article", "article_id")
    payload["citations_by_year"] = citation_history_json(payload.get("citations_by_year"))
    payload["is_vn_journal"] = is_vn_journal
    return int(
        conn.execute(
            text(
                'INSERT INTO "Article" ('
                '"issue_id", "title", "abstract", "publication_year", "doi", "primary_topic", "citation_count", "reference_count", '
                '"openalex_id", "landing_url", "pdf_url", "pages", "is_open_access", "citing_patents_count", "citations_by_year", "is_vn_journal"'
                ') VALUES ('
                ':issue_id, :title, :abstract, :publication_year, :doi, :primary_topic, :citation_count, :reference_count, '
                ':openalex_id, :landing_url, :pdf_url, :pages, :is_open_access, :citing_patents_count, CAST(:citations_by_year AS JSONB), :is_vn_journal'
                ') RETURNING "article_id"'
            ),
            payload,
        ).scalar_one()
    )
def upsert_author(conn, author_data: dict[str, Any], cache: dict) -> int:
    val = upsert_author_safe(conn, author_data, cache)
    last_known_institution = None
    last_known_institution_id = None
    institutions = author_data.get("institutions") or []
    if institutions:
        last_known_institution = institutions[0].get("display_name")
        last_known_institution_id = institutions[0].get("id")
    elif author_data.get("affiliation"):
        last_known_institution = author_data.get("affiliation")

    conn.execute(
        text(
            'UPDATE "Author" SET '
            '"last_known_institution" = COALESCE("last_known_institution", :inst), '
            '"last_known_institution_id" = COALESCE("last_known_institution_id", :inst_id) '
            'WHERE "author_id" = :author_id'
        ),
        {"inst": last_known_institution, "inst_id": last_known_institution_id, "author_id": val}
    )
    return val
def link_author_article(conn, author_id: int, article_id: int, author_position: str | None = None):
    row = conn.execute(
        text('SELECT "author_position" FROM "Author_Article" WHERE "author_id" = :author_id AND "article_id" = :article_id'),
        {"author_id": author_id, "article_id": article_id}
    ).fetchone()
    if not row:
        conn.execute(
            text('INSERT INTO "Author_Article" ("author_id", "article_id", "author_position") VALUES (:author_id, :article_id, :author_position)'),
            {"author_id": author_id, "article_id": article_id, "author_position": author_position}
        )
    else:
        if author_position is not None and row[0] != author_position:
            conn.execute(
                text('UPDATE "Author_Article" SET "author_position" = :author_position WHERE "author_id" = :author_id AND "article_id" = :article_id'),
                {"author_id": author_id, "article_id": article_id, "author_position": author_position}
            )


def upsert_keyword(conn, display_name: str, cache: dict) -> int:
    name_clean = display_name.strip()
    key = name_clean.lower()
    if key in cache:
        return cache[key]

    row = conn.execute(
        text('SELECT "keyword_id" FROM "Keyword" WHERE lower("display_name") = lower(:name) LIMIT 1'),
        {"name": name_clean}
    ).fetchone()
    if row:
        val = int(row[0])
        cache[key] = val
        return val

    sync_identity_sequence(conn, "Keyword", "keyword_id")
    val = int(
        conn.execute(
            text('INSERT INTO "Keyword" ("display_name") VALUES (:name) RETURNING "keyword_id"'),
            {"name": name_clean}
        ).scalar_one()
    )
    cache[key] = val
    return val


def link_keyword_article(conn, keyword_id: int, article_id: int, score: float | None = None):
    row = conn.execute(
        text('SELECT "score" FROM "Keyword_Article" WHERE "keyword_id" = :keyword_id AND "article_id" = :article_id'),
        {"keyword_id": keyword_id, "article_id": article_id}
    ).fetchone()
    if not row:
        conn.execute(
            text('INSERT INTO "Keyword_Article" ("keyword_id", "article_id", "score") VALUES (:keyword_id, :article_id, :score)'),
            {"keyword_id": keyword_id, "article_id": article_id, "score": score}
        )
    else:
        existing_score = row[0]
        if score is not None and (existing_score is None or existing_score < score):
            conn.execute(
                text('UPDATE "Keyword_Article" SET "score" = :score WHERE "keyword_id" = :keyword_id AND "article_id" = :article_id'),
                {"keyword_id": keyword_id, "article_id": article_id, "score": score}
            )


def backfill_related_doi_articles(conn, limit: int | None = None) -> dict[str, int]:
    """Scan existing relationship DOI rows and upsert linked Article records."""
    author_cache: dict[str, int] = {}
    keyword_cache: dict[str, int] = {}
    topic_cache: dict[str, int] = {}
    enrichment_caches = {
        "author_cache": author_cache,
        "keyword_cache": keyword_cache,
        "topic_cache": topic_cache,
        "openalex_work_by_doi": {},
        "openalex_work_by_id": {},
        "related_article_by_work": {},
    }
    stats = {
        "related_dois_seen": 0,
        "related_works_seen": 0,
        "related_works_fetched": 0,
        "related_articles_upserted": 0,
        "related_works_missing": 0,
        "related_dois_missing": 0,
        "related_identity_conflicts": 0,
        "citing_rows_linked": 0,
        "reference_rows_linked": 0,
        "institutions_found": 0,
        "institution_links_inserted": 0,
    }

    limit_clause = " LIMIT :limit" if limit else ""
    params = {"limit": limit} if limit else {}

    citing_rows = conn.execute(
        text(
            'SELECT "article_id", "openalex_work_id", "doi", "title", "publication_year", '
            '"source_name", "source_url", "landing_url", "pdf_url", "cited_by_count", "type" '
            'FROM "Article_Citing_Work" '
            'WHERE (("doi" IS NOT NULL AND "doi" <> \'\') OR ("openalex_work_id" IS NOT NULL AND "openalex_work_id" <> \'\')) '
            'AND "citing_article_id" IS NULL '
            'ORDER BY "updated_at" DESC NULLS LAST, "created_at" DESC NULLS LAST'
            + limit_clause
        ),
        params,
    ).mappings().all()

    total_citing = len(citing_rows)
    print(f"  [citing] {total_citing:,} citing rows to process", flush=True)
    t0 = time.time()
    for i, row in enumerate(citing_rows, 1):
        work = dict(row)
        citing_article_id = enrich_related_work_article(conn, work, enrichment_caches, stats)
        if citing_article_id:
            conn.execute(
                text(
                    'UPDATE "Article_Citing_Work" SET "citing_article_id" = :citing_article_id, "updated_at" = CURRENT_TIMESTAMP '
                    'WHERE "article_id" = :article_id AND "openalex_work_id" = :openalex_work_id'
                ),
                {
                    "citing_article_id": citing_article_id,
                    "article_id": row["article_id"],
                    "openalex_work_id": row["openalex_work_id"],
                },
            )
            stats["citing_rows_linked"] += 1
        if i % 25 == 0 or i == total_citing:
            elapsed = time.time() - t0
            print(
                f"  [citing] {i:,}/{total_citing:,}  "
                f"linked={stats['citing_rows_linked']:,}  "
                f"upserted={stats['related_articles_upserted']:,}  "
                f"missing={stats['related_works_missing']:,}  "
                f"{elapsed:.1f}s",
                flush=True,
            )

    remaining_limit = None
    if limit:
        remaining_limit = max(limit - len(citing_rows), 0)
    if remaining_limit is None or remaining_limit > 0:
        ref_limit_clause = " LIMIT :limit" if remaining_limit else ""
        ref_params = {"limit": remaining_limit} if remaining_limit else {}
        reference_rows = conn.execute(
            text(
                'SELECT "article_id", "reference_key", "openalex_work_id", "semantic_scholar_id", "doi", "title", '
                '"publication_year", "source_name", "source_url", "landing_url", "pdf_url", "cited_by_count", "type" '
                'FROM "Article_Reference" '
                'WHERE (("doi" IS NOT NULL AND "doi" <> \'\') OR ("openalex_work_id" IS NOT NULL AND "openalex_work_id" <> \'\')) '
                'AND "referenced_article_id" IS NULL '
                'ORDER BY "updated_at" DESC NULLS LAST, "created_at" DESC NULLS LAST'
                + ref_limit_clause
            ),
            ref_params,
        ).mappings().all()

        total_refs = len(reference_rows)
        print(f"  [reference] {total_refs:,} reference rows to process", flush=True)
        t1 = time.time()
        for j, row in enumerate(reference_rows, 1):
            work = dict(row)
            referenced_article_id = enrich_related_work_article(conn, work, enrichment_caches, stats)
            if referenced_article_id:
                conn.execute(
                    text(
                        'UPDATE "Article_Reference" SET "referenced_article_id" = :referenced_article_id, "updated_at" = CURRENT_TIMESTAMP '
                        'WHERE "article_id" = :article_id AND "reference_key" = :reference_key'
                    ),
                    {
                        "referenced_article_id": referenced_article_id,
                        "article_id": row["article_id"],
                        "reference_key": row["reference_key"],
                    },
                )
                stats["reference_rows_linked"] += 1
            if j % 25 == 0 or j == total_refs:
                elapsed = time.time() - t1
                print(
                    f"  [reference] {j:,}/{total_refs:,}  "
                    f"linked={stats['reference_rows_linked']:,}  "
                    f"upserted={stats['related_articles_upserted']:,}  "
                    f"missing={stats['related_works_missing']:,}  "
                    f"{elapsed:.1f}s",
                    flush=True,
                )

    total_elapsed = time.time() - t0
    print(
        f"  [backfill] Done in {total_elapsed:.1f}s  "
        f"seen={stats['related_works_seen']:,}  "
        f"fetched={stats['related_works_fetched']:,}  "
        f"upserted={stats['related_articles_upserted']:,}  "
            f"missing={stats['related_works_missing']:,}  "
            f"conflicts={stats['related_identity_conflicts']:,}  "
            f"citing_linked={stats['citing_rows_linked']:,}  "
        f"ref_linked={stats['reference_rows_linked']:,}",
        flush=True,
    )

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import ALL articles for one VN journal into Supabase")
    parser.add_argument("--json-file", type=Path, default=REPO_ROOT / "data" / "vietnam_journals" / "final" / "Acta_Mathematica_Vietnamica_openalex_final.json")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of articles to import (for debugging)")
    parser.add_argument("--backfill-related-dois", action="store_true", help="Only scan existing Article_Citing_Work/Article_Reference DOI rows and upsert linked Article records.")
    parser.add_argument("--related-limit", type=int, default=None, help="Limit DOI relationship rows processed by --backfill-related-dois.")
    args = parser.parse_args()

    load_env()

    if args.backfill_related_dois:
        supabase_url = get_supabase_url()
        print("Backfilling related DOI articles from existing relationship tables...", flush=True)
        engine = create_engine(supabase_url)
        with engine.begin() as conn:
            stats = backfill_related_doi_articles(conn, args.related_limit)
        print("[SUCCESS] Related DOI backfill completed", flush=True)
        print(
            "Related work enrichment: "
            f"seen={stats['related_works_seen']}, "
            f"fetched={stats['related_works_fetched']}, "
            f"upserted={stats['related_articles_upserted']}, "
            f"missing_or_failed={stats['related_works_missing']}, "
            f"identity_conflicts={stats['related_identity_conflicts']}, "
            f"citing_rows_linked={stats['citing_rows_linked']}, "
            f"reference_rows_linked={stats['reference_rows_linked']}, "
            f"institution_links_inserted={stats['institution_links_inserted']}",
            flush=True,
        )
        return

    package = load_package(args.json_file)
    journal_info = package.get("journal", {})
    articles = package.get("articles", [])

    if not articles:
        print("No articles found to import.")
        sys.exit(0)

    # 1. Prompt for limit interactively if not provided via --limit
    limit_val = args.limit
    if limit_val is None:
        try:
            user_limit = input(f"Phát hiện {len(articles)} bài báo. Nhập giới hạn (limit) bài báo muốn import (bỏ trống để import tất cả): ").strip()
            if user_limit:
                limit_val = int(user_limit)
        except Exception:
            limit_val = None

    if limit_val:
        articles_to_process = articles[:limit_val]
    else:
        articles_to_process = articles

    # 2. Preview trước
    print("\n" + "="*50)
    print("PREVIEW THÔNG TIN IMPORT:")
    print("="*50)
    print(f"Tên Tạp Chí (EN): {journal_info.get('name_en')}")
    print(f"Tên Tạp Chí (VN): {journal_info.get('name_vi')}")
    print(f"Mã Tạp Chí:       {journal_info.get('code')}")
    print(f"Nhà Xuất Bản:     {journal_info.get('university') or 'N/A'}")
    print(f"Tổng số bài báo:  {len(articles_to_process)} / {len(articles)}")
    
    print("\nMột số bài báo tiêu biểu:")
    for idx, art in enumerate(articles_to_process[:5], 1):
        print(f"  {idx}. {art.get('title')} ({art.get('publication_year')})")
        author_names = [a.get('name') for a in art.get('authors', []) or [] if a.get('name')]
        if author_names:
            print(f"     Tác giả: {', '.join(author_names[:3])}" + ("..." if len(author_names) > 3 else ""))
        raw_data = art.get("raw") or {}
        if raw_data.get("lens_id"):
            print(f"     Lens ID: {raw_data.get('lens_id')} | Patent Citations: {raw_data.get('lens_patent_citations') or 0} | Scholarly Citations: {raw_data.get('lens_scholarly_citations') or 0}")
    
    if len(articles_to_process) > 5:
        print(f"  ... và {len(articles_to_process) - 5} bài báo khác.")
    print("="*50 + "\n")

    # 3. Hỏi xác nhận import thật
    try:
        confirm = input("Bạn có chắc chắn muốn tiến hành import thật vào Supabase? (y/N): ").strip().lower()
    except Exception:
        confirm = 'n'

    if confirm != 'y':
        print("Đã hủy bỏ tiến trình import.")
        sys.exit(0)

    print(f"Starting import of {len(articles_to_process)} articles from {args.json_file.name}...", flush=True)

    supabase_url = get_supabase_url()
    print("Connecting to Supabase...", flush=True)
    engine = create_engine(supabase_url)

    # In-memory caches to save database roundtrips
    volume_cache = {}
    issue_cache = {}
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
    enrichment_stats = {
        "related_dois_seen": 0,
        "related_works_seen": 0,
        "related_works_fetched": 0,
        "related_articles_upserted": 0,
        "related_works_missing": 0,
        "related_dois_missing": 0,
        "related_identity_conflicts": 0,
        "institutions_found": 0,
        "institution_links_inserted": 0,
    }

    with engine.begin() as conn:
        # 1. Upsert Publisher & Country Zone & Journal
        print("Upserting Publisher and Journal registry mapping...", flush=True)
        publisher_id = upsert_publisher(conn, journal_info.get("publisher") or journal_info.get("university"))
        country_id = get_vn_country_zone(conn)
        journal_id = upsert_journal(conn, journal_info, publisher_id, country_id)
        print(f"Journal ID resolved: {journal_id}", flush=True)

        # 2. Iterate through all articles
        import_count = 0
        duplicate_set = set()

        for idx, article in enumerate(articles_to_process, 1):
            doi = article.get("doi")
            title = article.get("title")
            art_key = doi or title
            
            if not art_key:
                continue
                
            if art_key in duplicate_set:
                continue
            duplicate_set.add(art_key)

            print(f"[{idx}/{len(articles_to_process)}] Processing article: {title or doi or '<untitled>'}", flush=True)

            # Upsert Volume & Issue
            vol_id = upsert_volume(conn, journal_id, article.get("volume"), article.get("publication_year"), volume_cache)
            issue_id = upsert_issue(conn, vol_id, article.get("issue"), article.get("publication_year"), issue_cache)

            # Upsert Topics before Article so Article.primary_topic can be set
            oa_data = article.get("openalex") or {}
            primary_topic_ids = topic_ids_from_openalex_topic(conn, oa_data.get("primary_topic"), topic_cache)
            primary_topic_id = primary_topic_ids[0] if primary_topic_ids else None

            # Upsert Article
            article_id = upsert_article(conn, article, issue_id, primary_topic_id, is_vn_journal=True)

            # Upsert normalized OpenAlex citation relationships and enrich linked DOI articles.
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

            # Upsert & Link Authors
            for author_data in article.get("authors", []) or []:
                if not author_data.get("name"):
                    continue
                
                author_id = upsert_author(conn, author_data, author_cache)
                author_position = author_data.get("author_position")
                link_author_article(conn, author_id, article_id, author_position)
            affil_stats = persist_article_authorship_institutions(
                conn,
                article_id,
                article.get("publication_year"),
                article.get("authors", []) or [],
                author_cache,
            )
            enrichment_stats["institutions_found"] += affil_stats.institutions_found
            enrichment_stats["institution_links_inserted"] += affil_stats.institution_links_inserted

            # Upsert & Link Keywords
            oa_keywords = oa_data.get("keywords")
            if oa_keywords:
                for kw_obj in oa_keywords:
                    kw_name = kw_obj.get("display_name")
                    kw_score = kw_obj.get("score")
                    if not kw_name:
                        continue
                    kw_id = upsert_keyword(conn, kw_name, keyword_cache)
                    link_keyword_article(conn, kw_id, article_id, kw_score)
            else:
                for kw in article.get("keywords", []) or []:
                    if not kw:
                        continue
                    kw_id = upsert_keyword(conn, kw, keyword_cache)
                    link_keyword_article(conn, kw_id, article_id, None)

            # Upsert & Link OpenAlex Topics as Sub_Topic rows
            linked_topic_ids: set[int] = set(primary_topic_ids)
            for topic in oa_data.get("topics", []) or []:
                for topic_id in topic_ids_from_openalex_topic(conn, topic, topic_cache):
                    linked_topic_ids.add(topic_id)
            for topic_id in linked_topic_ids:
                link_sub_topic(conn, article_id, topic_id)

            import_count += 1
            if idx % 10 == 0 or idx == len(articles_to_process):
                print(f"Processed {idx}/{len(articles_to_process)} articles...", flush=True)

    print(f"\n[SUCCESS] Completed import of '{journal_info.get('name_en')}'!", flush=True)
    print(f"Imported/Updated unique articles: {import_count}", flush=True)
    print(
        "Related work enrichment: "
        f"seen={enrichment_stats['related_works_seen']}, "
        f"fetched={enrichment_stats['related_works_fetched']}, "
        f"upserted={enrichment_stats['related_articles_upserted']}, "
        f"missing_or_failed={enrichment_stats['related_works_missing']}, "
        f"identity_conflicts={enrichment_stats['related_identity_conflicts']}, "
        f"institution_links_inserted={enrichment_stats['institution_links_inserted']}",
        flush=True,
    )
    print(f"Stats cached in memory: {len(volume_cache)} Volumes, {len(issue_cache)} Issues, {len(author_cache)} Authors, {len(keyword_cache)} Keywords, {len(topic_cache)} Topics.", flush=True)


if __name__ == "__main__":
    main()
