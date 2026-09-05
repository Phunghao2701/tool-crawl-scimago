from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

for env_file in (".env.local", ".env", ".env.vercel"):
    load_dotenv(REPO_ROOT / env_file, override=False)

OPENALEX_WORK_BY_DOI = "https://api.openalex.org/works/doi:{doi}"
OPENALEX_WORKS = "https://api.openalex.org/works"


def build_headers(mailto: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    if mailto:
        headers["User-Agent"] += f" (mailto:{mailto})"
    return headers


def openalex_params(mailto: str | None = None, **extra: Any) -> dict[str, Any]:
    params = {key: value for key, value in extra.items() if value not in (None, "")}
    polite_email = mailto or os.getenv("OPENALEX_EMAIL")
    api_key = os.getenv("OPENALEX_API_KEY")
    if polite_email:
        params["mailto"] = polite_email
    if api_key:
        params["api_key"] = api_key
    return params


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_doi(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(".") or None


def normalize_name(value: Any) -> str:
    text = clean_text(value) or ""
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def extract_doi(article: dict[str, Any]) -> str | None:
    doi = normalize_doi(article.get("doi"))
    if doi:
        return doi
    for key in ("source_url", "landing_url"):
        url = str(article.get(key) or "")
        match = re.search(r"(?:doi\.org/|/doi/(?:abs/|full/)?)(10\.\d{4,9}/\S+)", url, re.IGNORECASE)
        if match:
            return normalize_doi(match.group(1))
    return None


def fetch_openalex_work(doi: str, mailto: str | None = None) -> dict[str, Any] | None:
    url = OPENALEX_WORK_BY_DOI.format(doi=quote(doi, safe="/"))
    params = openalex_params(mailto)
    try:
        response = requests.get(url, headers=build_headers(mailto), params=params, timeout=30)
        if response.status_code == 404:
            print(f"  [OpenAlex] DOI not found: {doi}")
            return None
        if response.status_code != 200:
            print(f"  [OpenAlex] HTTP {response.status_code} for DOI {doi}: {response.text[:200]}")
            return None
        return response.json()
    except Exception as exc:
        print(f"  [OpenAlex] Failed for DOI {doi}: {exc}")
        return None


def fetch_openalex_works_by_ids(work_ids: list[str], mailto: str | None = None) -> list[dict[str, Any]]:
    """Fetch OpenAlex works by IDs using the pipe-separated OR filter."""
    cleaned = [work_id for work_id in work_ids if work_id]
    if not cleaned:
        return []
    params = openalex_params(
        mailto,
        filter="openalex:" + "|".join(cleaned),
        **{"per-page": min(len(cleaned), 200)},
    )
    try:
        response = requests.get(OPENALEX_WORKS, headers=build_headers(mailto), params=params, timeout=45)
        if response.status_code != 200:
            print(f"  [OpenAlex] HTTP {response.status_code} for reference batch: {response.text[:200]}")
            return []
        return response.json().get("results") or []
    except Exception as exc:
        print(f"  [OpenAlex] Reference batch failed: {exc}")
        return []


def fetch_openalex_citing_works(work_id: str | None, limit: int = 0, mailto: str | None = None) -> list[dict[str, Any]]:
    """Fetch works that cite the given OpenAlex work ID."""
    if not work_id or limit <= 0:
        return []
    params = openalex_params(
        mailto,
        filter=f"cites:{work_id}",
        **{"per-page": min(limit, 200), "sort": "cited_by_count:desc"},
    )
    try:
        response = requests.get(OPENALEX_WORKS, headers=build_headers(mailto), params=params, timeout=45)
        if response.status_code != 200:
            print(f"  [OpenAlex] HTTP {response.status_code} for citing works {work_id}: {response.text[:200]}")
            return []
        return (response.json().get("results") or [])[:limit]
    except Exception as exc:
        print(f"  [OpenAlex] Citing works failed for {work_id}: {exc}")
        return []


def extract_meta_values(html_text: str, meta_name: str) -> list[str]:
    pattern = re.compile(
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(meta_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    return [html.unescape(match).strip() for match in pattern.findall(html_text) if match.strip()]


def fetch_landing_metadata(url: str | None) -> dict[str, Any]:
    if not url:
        return {}
    try:
        response = requests.get(url, headers=build_headers(), timeout=30, allow_redirects=True)
        if response.status_code != 200:
            print(f"  [Landing] HTTP {response.status_code} for {url}")
            return {}
    except Exception as exc:
        print(f"  [Landing] Failed for {url}: {exc}")
        return {}

    html_text = response.text
    abstract_values = (
        extract_meta_values(html_text, "citation_abstract")
        or extract_meta_values(html_text, "dc.description")
        or extract_meta_values(html_text, "description")
        or extract_meta_values(html_text, "og:description")
    )
    pdf_values = extract_meta_values(html_text, "citation_pdf_url")
    abstract = clean_text(abstract_values[0]) if abstract_values else None
    pdf_url = clean_text(pdf_values[0]) if pdf_values else None
    return {
        "landing_url": response.url,
        "abstract": abstract,
        "pdf_url": pdf_url,
    }


def simplify_institution(inst: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": inst.get("id"),
        "display_name": inst.get("display_name"),
        "ror": inst.get("ror"),
        "country_code": inst.get("country_code"),
        "type": inst.get("type"),
    }


def simplify_author(authorship: dict[str, Any]) -> dict[str, Any]:
    author = authorship.get("author") or {}
    institutions = [simplify_institution(inst) for inst in authorship.get("institutions") or []]
    institution_names = [inst.get("display_name") for inst in institutions if inst.get("display_name")]
    countries = [country for country in authorship.get("countries") or [] if country]
    return {
        "name": clean_text(author.get("display_name")),
        "openalex_author_id": author.get("id"),
        "orcid": author.get("orcid"),
        "author_position": authorship.get("author_position"),
        "is_corresponding": authorship.get("is_corresponding"),
        "raw_affiliation_strings": authorship.get("raw_affiliation_strings") or [],
        "institutions": institutions,
        "institution_names": institution_names,
        "countries": countries,
    }


def reconstruct_abstract(work: dict[str, Any]) -> str | None:
    inverted_index = work.get("abstract_inverted_index") or {}
    if not isinstance(inverted_index, dict) or not inverted_index:
        return clean_text(work.get("abstract"))

    tokens: list[tuple[int, str]] = []
    for token, positions in inverted_index.items():
        if not token or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                tokens.append((pos, str(token)))

    if not tokens:
        return clean_text(work.get("abstract"))

    tokens.sort(key=lambda item: item[0])
    return clean_text(" ".join(token for _, token in tokens))


def extract_pdf_url(work: dict[str, Any]) -> str | None:
    location_candidates = [
        work.get("best_oa_location"),
        work.get("primary_location"),
        *(work.get("locations") or []),
    ]
    seen: set[str] = set()
    for location in location_candidates:
        if not isinstance(location, dict):
            continue
        for key in ("pdf_url", "landing_page_url"):
            value = clean_text(location.get(key))
            if value and value.lower().startswith("http") and value not in seen:
                seen.add(value)
                if key == "pdf_url" or value.lower().endswith(".pdf"):
                    return value
        pdf_url = clean_text((location.get("pdf_url") if isinstance(location, dict) else None))
        if pdf_url and pdf_url.lower().startswith("http"):
            return pdf_url

    primary_location = work.get("primary_location") or {}
    primary_pdf = clean_text(primary_location.get("pdf_url"))
    if primary_pdf and primary_pdf.lower().startswith("http"):
        return primary_pdf
    return None


def simplify_work(work: dict[str, Any], doi: str | None = None) -> dict[str, Any]:
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    authors = []
    for authorship in work.get("authorships") or []:
        author = simplify_author(authorship)
        if author.get("name"):
            authors.append(author)
    return {
        "work_id": work.get("id"),
        "doi": work.get("doi") or (f"https://doi.org/{doi}" if doi else None),
        "title": work.get("title"),
        "abstract": reconstruct_abstract(work),
        "pdf_url": extract_pdf_url(work),
        "landing_url": primary_location.get("landing_page_url"),
        "publication_year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count"),
        "counts_by_year": work.get("counts_by_year"),
        "open_access": work.get("open_access"),
        "best_oa_location": work.get("best_oa_location"),
        "referenced_works": work.get("referenced_works") or [],
        "referenced_works_count": len(work.get("referenced_works") or []),
        "primary_topic": work.get("primary_topic"),
        "topics": work.get("topics"),
        "keywords": work.get("keywords"),
        "type": work.get("type"),
        "authors": authors,
        "source": {
            "id": source.get("id"),
            "display_name": source.get("display_name"),
            "issn_l": source.get("issn_l"),
            "issn": source.get("issn"),
            "host_organization": source.get("host_organization"),
            "host_organization_name": source.get("host_organization_name"),
            "type": source.get("type"),
        },
    }


def article_authors_as_objects(article: dict[str, Any]) -> list[dict[str, Any]]:
    authors = article.get("authors") or []
    normalized: list[dict[str, Any]] = []
    for author in authors:
        if isinstance(author, dict):
            item = dict(author)
            item["name"] = clean_text(item.get("name"))
            if item.get("name"):
                normalized.append(item)
        else:
            name = clean_text(author)
            if name:
                normalized.append({"name": name})
    return normalized


def match_openalex_author(author: dict[str, Any], openalex_authors: list[dict[str, Any]], used_indexes: set[int], preferred_index: int) -> tuple[int | None, dict[str, Any] | None]:
    if preferred_index < len(openalex_authors) and preferred_index not in used_indexes:
        return preferred_index, openalex_authors[preferred_index]

    target = normalize_name(author.get("name"))
    if not target:
        return None, None
    for idx, candidate in enumerate(openalex_authors):
        if idx in used_indexes:
            continue
        if normalize_name(candidate.get("name")) == target:
            return idx, candidate
    return None, None


def merge_authors_with_openalex(article: dict[str, Any], openalex_authors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base_authors = article_authors_as_objects(article)
    if not base_authors and openalex_authors:
        base_authors = [{"name": author.get("name")} for author in openalex_authors if author.get("name")]

    used_indexes: set[int] = set()
    merged: list[dict[str, Any]] = []
    for idx, author in enumerate(base_authors):
        item = dict(author)
        match_idx, openalex_author = match_openalex_author(author, openalex_authors, used_indexes, idx)
        if openalex_author:
            used_indexes.add(match_idx)  # type: ignore[arg-type]
            item.update({key: value for key, value in openalex_author.items() if value not in (None, [], "")})
            if not item.get("affiliation") and openalex_author.get("institution_names"):
                item["affiliation"] = "; ".join(openalex_author["institution_names"])
        merged.append(item)

    for idx, openalex_author in enumerate(openalex_authors):
        if idx not in used_indexes and openalex_author.get("name"):
            extra = {key: value for key, value in openalex_author.items() if value not in (None, [], "")}
            if extra.get("institution_names") and not extra.get("affiliation"):
                extra["affiliation"] = "; ".join(extra["institution_names"])
            merged.append(extra)

    return merged


def enrich_article(
    article: dict[str, Any],
    mailto: str | None = None,
    citing_limit: int = 0,
    references_limit: int = 0,
    enrich_references: bool = True,
) -> tuple[dict[str, Any], bool]:
    doi = extract_doi(article)
    if not doi:
        return article, False

    work = fetch_openalex_work(doi, mailto=mailto)
    if not work:
        raw = dict(article.get("raw") or {})
        raw["openalex_lookup"] = {"doi": doi, "status": "not_found_or_failed"}
        merged = dict(article)
        merged["raw"] = raw
        return merged, False

    openalex_authors = [simplify_author(authorship) for authorship in work.get("authorships") or []]
    openalex_work = simplify_work(work, doi)
    landing_metadata: dict[str, Any] = {}
    merged = dict(article)
    merged["doi"] = doi
    merged["authors"] = merge_authors_with_openalex(article, openalex_authors)

    if not openalex_work.get("abstract") or not openalex_work.get("pdf_url"):
        landing_url = article.get("landing_url") or article.get("source_url") or work.get("doi") or f"https://doi.org/{doi}"
        landing_metadata = fetch_landing_metadata(str(landing_url))
        if not openalex_work.get("abstract") and landing_metadata.get("abstract"):
            openalex_work["abstract"] = landing_metadata["abstract"]
        if not openalex_work.get("pdf_url") and landing_metadata.get("pdf_url"):
            openalex_work["pdf_url"] = landing_metadata["pdf_url"]
        if landing_metadata.get("landing_url"):
            openalex_work["landing_url"] = landing_metadata["landing_url"]

    if not clean_text(merged.get("abstract")) and openalex_work.get("abstract"):
        merged["abstract"] = openalex_work["abstract"]
    if not clean_text(merged.get("pdf_url")) and openalex_work.get("pdf_url"):
        merged["pdf_url"] = openalex_work["pdf_url"]

    referenced_ids = list(openalex_work.get("referenced_works") or [])
    if enrich_references and references_limit > 0 and referenced_ids:
        reference_batch = fetch_openalex_works_by_ids(referenced_ids[:references_limit], mailto=mailto)
        openalex_work["referenced_works_enriched"] = [simplify_work(item) for item in reference_batch]

    if citing_limit > 0:
        citing_batch = fetch_openalex_citing_works(openalex_work.get("work_id"), limit=citing_limit, mailto=mailto)
        openalex_work["citing_works"] = [simplify_work(item) for item in citing_batch]

    merged["openalex"] = openalex_work

    raw = dict(article.get("raw") or {})
    raw["openalex_authorships"] = openalex_authors
    raw["openalex_lookup"] = {"doi": doi, "status": "ok", "work_id": work.get("id")}
    if landing_metadata:
        raw["openalex_landing_metadata"] = landing_metadata
    merged["raw"] = raw
    return merged, True


def enrich_package(
    data: list[dict[str, Any]],
    limit: int = 0,
    delay: float = 0.2,
    mailto: str | None = None,
    citing_limit: int = 0,
    references_limit: int = 0,
    enrich_references: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    total_seen = 0
    total_enriched = 0
    output: list[dict[str, Any]] = []

    for journal_pkg in data:
        journal = dict(journal_pkg.get("journal") or {})
        articles = journal_pkg.get("articles") or []
        enriched_articles: list[dict[str, Any]] = []
        print(f"\nProcessing journal: {journal.get('code') or 'unknown'} ({len(articles)} articles)")

        for idx, article in enumerate(articles):
            if limit > 0 and total_seen >= limit:
                print(f"Reached limit of {limit} OpenAlex lookups, keeping remaining articles as-is.")
                enriched_articles.extend(articles[idx:])
                break

            doi = extract_doi(article)
            if not doi:
                enriched_articles.append(article)
                continue

            total_seen += 1
            print(f"[{idx + 1}/{len(articles)}] OpenAlex DOI enrich: {doi}")
            enriched_article, changed = enrich_article(
                article,
                mailto=mailto,
                citing_limit=citing_limit,
                references_limit=references_limit,
                enrich_references=enrich_references,
            )
            enriched_articles.append(enriched_article)
            if changed:
                total_enriched += 1
            if delay > 0:
                time.sleep(delay)
        else:
            pass

        output.append({**journal_pkg, "journal": journal, "articles": enriched_articles})

    return output, total_seen, total_enriched


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich VN journal JSON with OpenAlex metadata by article DOI.")
    parser.add_argument("input_json", help="Path to VN preview/final JSON")
    parser.add_argument("output_json", help="Path to enriched output JSON")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of DOI lookups for testing")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between OpenAlex requests in seconds")
    parser.add_argument("--mailto", help="Email for OpenAlex polite pool")
    parser.add_argument("--citing-limit", type=int, default=0, help="Max citing works to fetch per article; 0 disables")
    parser.add_argument("--references-limit", type=int, default=200, help="Max references to enrich per article")
    parser.add_argument("--skip-reference-enrichment", action="store_true", help="Keep reference IDs only without fetching metadata")
    args = parser.parse_args()

    input_path = Path(args.input_json)
    output_path = Path(args.output_json)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Input JSON must be a package object or list of package objects")

    enriched, total_seen, total_enriched = enrich_package(
        data,
        limit=args.limit,
        delay=args.delay,
        mailto=args.mailto,
        citing_limit=args.citing_limit,
        references_limit=args.references_limit,
        enrich_references=not args.skip_reference_enrichment,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone! OpenAlex DOI lookups: {total_seen}")
    print(f"Articles enriched from OpenAlex: {total_enriched}")
    print(f"Saved enriched JSON to: {output_path}")


if __name__ == "__main__":
    main()
