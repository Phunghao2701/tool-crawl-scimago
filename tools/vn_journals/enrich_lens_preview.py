from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import urllib3

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def build_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def split_semicolon(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


def fetch_response(url: str) -> requests.Response | None:
    try:
        res = requests.get(url, headers=build_headers(), timeout=20, allow_redirects=True, verify=False)
        if res.status_code == 200:
            return res
        print(f"  [Warning] Unexpected status {res.status_code} for {url}")
    except Exception as exc:
        print(f"  [Warning] Failed to fetch {url}: {exc}")
    return None


def extract_meta_values(html_text: str, meta_name: str) -> list[str]:
    pattern = re.compile(
        rf'<meta[^>]+name=["\']{re.escape(meta_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    return [html.unescape(match).strip() for match in pattern.findall(html_text) if match.strip()]


def extract_pdf_url(html_text: str, final_url: str) -> str | None:
    meta_pdf = extract_meta_values(html_text, "citation_pdf_url")
    if meta_pdf:
        return meta_pdf[0]

    pdf_href_match = re.search(r'href=["\']([^"\']+/article/download/[^"\']+)["\']', html_text, re.IGNORECASE)
    if pdf_href_match:
        return urljoin(final_url, html.unescape(pdf_href_match.group(1)))

    return None


def merge_authors(article: dict[str, Any], html_text: str) -> list[dict[str, Any]]:
    lens_authors = article.get("authors") or []
    official_authors = extract_meta_values(html_text, "citation_author")
    official_affiliations = extract_meta_values(html_text, "citation_author_institution")

    names = official_authors or lens_authors
    merged: list[dict[str, Any]] = []
    for idx, name in enumerate(names):
        item = {"name": clean_text(name)}
        if idx < len(official_affiliations):
            item["affiliation"] = clean_text(official_affiliations[idx])
        merged.append(item)
    return [item for item in merged if item.get("name")]


def enrich_article(article: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    source_url = article.get("source_url")
    if not source_url:
        return article, False

    response = fetch_response(source_url)
    if not response:
        return article, False

    html_text = response.text
    final_url = response.url

    official_keywords = extract_meta_values(html_text, "citation_keywords")
    official_abstract = extract_meta_values(html_text, "citation_abstract")
    official_issn = extract_meta_values(html_text, "citation_issn")
    pdf_url = article.get("pdf_url") or extract_pdf_url(html_text, final_url)

    lens_fields = ((article.get("raw") or {}).get("lens_fields_of_study"))
    fallback_keywords = split_semicolon(lens_fields)

    merged = dict(article)
    merged["source_url"] = final_url
    merged["pdf_url"] = pdf_url
    merged["abstract"] = clean_text(official_abstract[0]) if official_abstract else article.get("abstract")
    merged["keywords"] = official_keywords or article.get("keywords") or fallback_keywords
    merged["authors"] = [item["name"] for item in merge_authors(article, html_text)]

    raw = dict(article.get("raw") or {})
    raw["official_landing_url"] = final_url
    raw["official_pdf_url"] = pdf_url
    raw["official_keywords"] = official_keywords
    raw["official_authors"] = merge_authors(article, html_text)
    raw["official_abstract"] = clean_text(official_abstract[0]) if official_abstract else None
    raw["official_issn"] = official_issn[0] if official_issn else None
    merged["raw"] = raw

    return merged, True


def enrich_journal_meta(journal: dict[str, Any], articles: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(journal)
    enriched["platform"] = "lens_plus_official"

    official_issn = None
    for article in articles:
        raw = article.get("raw") or {}
        if raw.get("official_issn"):
            official_issn = raw["official_issn"]
            break

    notes = str(enriched.get("notes") or "")
    if official_issn:
        extra_note = f" | Official ISSN observed: {official_issn}"
        if extra_note not in notes:
            notes += extra_note
    enriched["notes"] = notes.strip()
    return enriched


def build_final_package(journal_pkg: dict[str, Any], enriched_articles: list[dict[str, Any]]) -> dict[str, Any]:
    journal = enrich_journal_meta(journal_pkg["journal"], enriched_articles)
    final_articles: list[dict[str, Any]] = []

    for article in enriched_articles:
        raw = article.get("raw") or {}
        final_articles.append(
            {
                "source_journal_code": article.get("source_journal_code"),
                "title": article.get("title"),
                "doi": article.get("doi"),
                "landing_url": raw.get("official_landing_url") or article.get("source_url"),
                "source_url": article.get("source_url"),
                "pdf_url": article.get("pdf_url"),
                "authors": raw.get("official_authors")
                or [{"name": name} for name in article.get("authors", [])],
                "abstract": article.get("abstract"),
                "keywords": article.get("keywords") or [],
                "volume": article.get("volume"),
                "issue": article.get("issue"),
                "pages": article.get("pages"),
                "publication_year": article.get("publication_year"),
                "language": article.get("language"),
                "lens": {
                    "lens_id": raw.get("lens_id"),
                    "citing_works_count": raw.get("lens_scholarly_citations"),
                    "citing_patents_count": raw.get("lens_patent_citations"),
                    "is_open_access": raw.get("lens_open_access"),
                    "open_access_license": raw.get("lens_open_access_license"),
                    "open_access_colour": raw.get("lens_open_access_colour"),
                    "fields_of_study": raw.get("lens_fields_of_study"),
                },
                "official": {
                    "landing_url": raw.get("official_landing_url"),
                    "pdf_url": raw.get("official_pdf_url"),
                    "keywords": raw.get("official_keywords") or [],
                    "abstract": raw.get("official_abstract"),
                    "issn": raw.get("official_issn"),
                },
                "raw": raw,
            }
        )

    return {"journal": journal, "articles": final_articles}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create final VN journal JSON by enriching Lens preview with official journal metadata.")
    parser.add_argument("input_json", help="Path to lens preview JSON")
    parser.add_argument("output_json", help="Path to final JSON")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of article requests for testing")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests in seconds")
    args = parser.parse_args()

    input_path = Path(args.input_json)
    output_path = Path(args.output_json)

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    data = json.loads(input_path.read_text(encoding="utf-8"))

    final_data: list[dict[str, Any]] = []
    total_requests = 0
    total_enriched = 0

    for journal_pkg in data:
        journal_code = journal_pkg["journal"]["code"]
        articles = journal_pkg["articles"]
        print(f"\nProcessing journal: {journal_code} ({len(articles)} articles)")

        enriched_articles: list[dict[str, Any]] = []
        for idx, article in enumerate(articles):
            if args.limit > 0 and total_requests >= args.limit:
                print(f"Reached limit of {args.limit} requests, keeping remaining articles as-is.")
                enriched_articles.extend(articles[idx:])
                break

            if article.get("source_url"):
                print(f"[{idx + 1}/{len(articles)}] Enriching: {str(article.get('title') or '')[:70]}...")
                enriched_article, changed = enrich_article(article)
                enriched_articles.append(enriched_article)
                total_requests += 1
                if changed:
                    total_enriched += 1
                time.sleep(args.delay)
            else:
                enriched_articles.append(article)
        else:
            pass

        final_data.append(build_final_package(journal_pkg, enriched_articles))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(final_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone! Requests sent: {total_requests}")
    print(f"Articles enriched from official pages: {total_enriched}")
    print(f"Saved final JSON to: {output_path}")


if __name__ == "__main__":
    main()
