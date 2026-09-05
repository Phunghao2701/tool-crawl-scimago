from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from tools.vn_journals.paper_vn_affiliations import clean_text, normalize_openalex_id


PROTECTED_SCALAR_FIELDS = (
    "abstract",
    "publication_year",
    "issue_id",
    "primary_topic",
    "openalex_id",
    "landing_url",
    "pdf_url",
    "pages",
    "is_open_access",
    "citing_patents_count",
)


def normalize_doi(value: Any) -> str | None:
    text_value = clean_text(value)
    if not text_value:
        return None
    text_value = re.sub(r"^https?://(dx\.)?doi\.org/", "", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"^doi:\s*", "", text_value, flags=re.IGNORECASE)
    return text_value.strip().rstrip(".").lower() or None


def normalize_work_id(value: Any) -> str | None:
    return normalize_openalex_id(value, "work")


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text_value = str(value).strip().lower()
    if text_value in {"true", "1", "yes", "y"}:
        return True
    if text_value in {"false", "0", "no", "n"}:
        return False
    return None


def normalize_citation_history(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    items: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                items.append((item.get("year"), item.get("cited_by_count", item.get("count"))))
    else:
        return None

    normalized: dict[str, int] = {}
    for year, count in items:
        try:
            year_key = str(int(year))
            count_value = int(count)
        except (TypeError, ValueError):
            continue
        if count_value < 0:
            continue
        normalized[year_key] = max(normalized.get(year_key, 0), count_value)
    return normalized or None


def merge_citation_history(existing: Any, incoming: Any) -> dict[str, int] | None:
    existing_history = normalize_citation_history(existing) or {}
    incoming_history = normalize_citation_history(incoming) or {}
    if not incoming_history:
        return existing_history or None
    merged = dict(existing_history)
    for year, count in incoming_history.items():
        merged[year] = max(int(merged.get(year, 0)), int(count))
    return merged or None


def extract_best_oa(work_or_oa: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(work_or_oa, dict):
        return {"is_oa": None, "landing_url": None, "pdf_url": None}
    open_access = work_or_oa.get("open_access") if "open_access" in work_or_oa else work_or_oa
    if not isinstance(open_access, dict):
        open_access = {}
    location = work_or_oa.get("best_oa_location") if isinstance(work_or_oa.get("best_oa_location"), dict) else {}
    return {
        "is_oa": parse_bool(open_access.get("is_oa")),
        "landing_url": clean_text(location.get("landing_page_url")),
        "pdf_url": clean_text(location.get("pdf_url")),
    }


def article_open_access(article: dict[str, Any]) -> bool | None:
    oa_data = article.get("openalex") or {}
    oa_from_openalex = extract_best_oa(oa_data).get("is_oa")
    if isinstance(oa_from_openalex, bool):
        return oa_from_openalex
    is_oa_raw = (article.get("lens") or {}).get("is_open_access")
    if is_oa_raw is None:
        is_oa_raw = (article.get("raw") or {}).get("lens_open_access")
    return parse_bool(is_oa_raw)


def article_urls(article: dict[str, Any]) -> tuple[str | None, str | None]:
    oa_data = article.get("openalex") or {}
    best_oa = extract_best_oa(oa_data)
    landing_url = (
        best_oa.get("landing_url")
        or clean_text(article.get("landing_url"))
        or clean_text(oa_data.get("landing_url"))
        or clean_text(article.get("source_url"))
    )
    pdf_url = best_oa.get("pdf_url") or clean_text(article.get("pdf_url")) or clean_text(oa_data.get("pdf_url"))
    return landing_url, pdf_url


def build_article_payload(article: dict[str, Any], issue_id: int | None, primary_topic_id: int | None = None) -> dict[str, Any]:
    oa_data = article.get("openalex") or {}
    landing_url, pdf_url = article_urls(article)
    citing_patents_raw = (article.get("lens") or {}).get("citing_patents_count")
    if citing_patents_raw is None:
        citing_patents_raw = (article.get("raw") or {}).get("lens_patent_citations")
    citing_patents_count = None
    if citing_patents_raw is not None:
        try:
            citing_patents_count = int(citing_patents_raw)
        except (TypeError, ValueError):
            citing_patents_count = None

    history = normalize_citation_history(oa_data.get("counts_by_year"))
    return {
        "issue_id": issue_id,
        "title": clean_text(article.get("title")),
        "abstract": clean_text(article.get("abstract")),
        "publication_year": article.get("publication_year"),
        "doi": normalize_doi(article.get("doi")),
        "primary_topic": primary_topic_id,
        "citation_count": oa_data.get("cited_by_count"),
        "reference_count": oa_data.get("referenced_works_count"),
        "openalex_id": normalize_work_id(oa_data.get("work_id") or article.get("openalex_id")),
        "landing_url": landing_url,
        "pdf_url": pdf_url,
        "pages": clean_text(article.get("pages")),
        "is_open_access": article_open_access(article),
        "citing_patents_count": citing_patents_count,
        "citations_by_year": history,
    }


def merge_article_values(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for field in PROTECTED_SCALAR_FIELDS:
        if incoming.get(field) is not None:
            merged[field] = incoming[field]
    for metric in ("citation_count", "reference_count"):
        old_value = existing.get(metric)
        new_value = incoming.get(metric)
        if old_value is None:
            merged[metric] = new_value
        elif new_value is None:
            merged[metric] = old_value
        else:
            merged[metric] = max(int(old_value), int(new_value))
    history = merge_citation_history(existing.get("citations_by_year"), incoming.get("citations_by_year"))
    merged["citations_by_year"] = history
    if incoming.get("doi") is not None:
        merged["doi"] = incoming["doi"]
    if incoming.get("title") is not None:
        merged["title"] = incoming["title"]
    return merged


def safe_title_year_candidate_ids(candidates: list[Any], incoming_doi: Any, incoming_openalex_id: Any) -> list[int]:
    doi = normalize_doi(incoming_doi)
    openalex_id = normalize_work_id(incoming_openalex_id)
    safe_ids: list[int] = []
    for candidate in candidates:
        if hasattr(candidate, "_mapping"):
            candidate = candidate._mapping
        if isinstance(candidate, Mapping):
            article_id = candidate.get("article_id")
            candidate_doi = candidate.get("doi")
            candidate_openalex_id = candidate.get("openalex_id")
        else:
            article_id = candidate[0]
            candidate_doi = candidate[1]
            candidate_openalex_id = candidate[2]
        normalized_candidate_doi = normalize_doi(candidate_doi)
        normalized_candidate_openalex_id = normalize_work_id(candidate_openalex_id)
        if doi and normalized_candidate_doi and normalized_candidate_doi != doi:
            continue
        if openalex_id and normalized_candidate_openalex_id and normalized_candidate_openalex_id != openalex_id:
            continue
        safe_ids.append(int(article_id))
    return safe_ids if len(safe_ids) == 1 else []


def citation_history_json(value: Any) -> str | None:
    history = normalize_citation_history(value)
    return json.dumps(history, ensure_ascii=False) if history else None
