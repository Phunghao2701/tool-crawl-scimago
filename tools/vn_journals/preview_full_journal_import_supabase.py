from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.import_one_journal_supabase import normalize_issn
from tools.vn_journals.paper_vn_article_metadata import article_open_access, normalize_citation_history
from tools.vn_journals.paper_vn_affiliations import normalize_institution_payload

DEFAULT_INPUT = REPO_ROOT / "data" / "vietnam_journals" / "final" / "Acta_Mathematica_Vietnamica_openalex_final.json"


def load_package(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unsupported VN journal JSON structure: {path}")


def unique_nonempty(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text_value = str(value or "").strip()
        if text_value and text_value not in seen:
            seen.add(text_value)
            result.append(text_value)
    return result


def article_key(article: dict[str, Any]) -> str:
    return str(article.get("doi") or article.get("title") or "").strip()


def build_preview(package: dict[str, Any], limit_examples: int, limit_articles: int | None = None) -> dict[str, Any]:
    journal = package.get("journal", {})
    articles = package.get("articles", [])
    if limit_articles:
        articles = articles[:limit_articles]

    unique_articles = []
    seen_article_keys: set[str] = set()
    duplicate_article_count = 0
    for article in articles:
        key = article_key(article)
        if key and key in seen_article_keys:
            duplicate_article_count += 1
            continue
        if key:
            seen_article_keys.add(key)
        unique_articles.append(article)

    volume_values = unique_nonempty([article.get("volume") for article in unique_articles])
    issue_values = unique_nonempty([
        f"{article.get('volume') or ''}::{article.get('issue') or ''}::{article.get('publication_year') or ''}"
        for article in unique_articles
        if article.get("issue") or article.get("volume") or article.get("publication_year")
    ])

    author_keys: list[str] = []
    keyword_values: list[str] = []
    article_author_links = 0
    keyword_article_links = 0
    articles_with_abstract = 0
    articles_with_openalex = 0
    articles_with_pdf = 0
    citing_work_links = 0
    reference_links = 0
    authorship_institution_count = 0
    institution_keys: set[tuple[str | None, str, str | None, str | None]] = set()
    institution_author_links = 0
    oa_true = 0
    oa_false = 0
    oa_unavailable = 0
    articles_with_citation_history = 0
    articles_without_citation_history = 0
    year_counter: Counter[int] = Counter()

    preview_articles: list[dict[str, Any]] = list(unique_articles)
    for article in unique_articles:
        openalex = article.get("openalex") or {}
        preview_articles.extend(openalex.get("citing_works", []) or [])
        preview_articles.extend(openalex.get("referenced_works_enriched", []) or [])

    for article in preview_articles:
        if article.get("abstract"):
            articles_with_abstract += 1
        if article.get("openalex"):
            articles_with_openalex += 1
            openalex = article.get("openalex") or {}
            citing_work_links += len(openalex.get("citing_works", []) or [])
            reference_links += len(openalex.get("referenced_works_enriched", []) or openalex.get("referenced_works", []) or [])
        elif article.get("id") or article.get("work_id"):
            articles_with_openalex += 1
            openalex = article
        else:
            openalex = {}
        if article.get("pdf_url"):
            articles_with_pdf += 1
        if article.get("publication_year"):
            year_counter[int(article["publication_year"])] += 1
        oa_value = article_open_access(article if article.get("openalex") else {"openalex": article})
        if oa_value is True:
            oa_true += 1
        elif oa_value is False:
            oa_false += 1
        else:
            oa_unavailable += 1
        if normalize_citation_history(openalex.get("counts_by_year")):
            articles_with_citation_history += 1
        else:
            articles_without_citation_history += 1

        for author in article.get("authors", []) or []:
            if isinstance(author, dict):
                key = author.get("openalex_author_id") or author.get("orcid") or author.get("name")
                for institution in author.get("institutions") or []:
                    normalized = normalize_institution_payload(institution)
                    if not normalized:
                        continue
                    authorship_institution_count += 1
                    institution_author_links += 1
                    institution_keys.add(
                        (
                            normalized["openalex_id"],
                            normalized["display_name"].casefold(),
                            normalized["country_code"],
                            normalized["type"],
                        )
                    )
            else:
                key = str(author)
            if key:
                author_keys.append(str(key))
                article_author_links += 1
        for authorship in article.get("authorships", []) or []:
            author = authorship.get("author") or {}
            key = author.get("id") or author.get("orcid") or author.get("display_name")
            if key:
                author_keys.append(str(key))
                article_author_links += 1
            for institution in authorship.get("institutions") or []:
                normalized = normalize_institution_payload(institution)
                if not normalized:
                    continue
                authorship_institution_count += 1
                institution_author_links += 1
                institution_keys.add(
                    (
                        normalized["openalex_id"],
                        normalized["display_name"].casefold(),
                        normalized["country_code"],
                        normalized["type"],
                    )
                )

        for keyword in article.get("keywords", []) or []:
            if keyword:
                keyword_values.append(str(keyword))
                keyword_article_links += 1

    sample_articles = []
    for article in unique_articles[:limit_examples]:
        openalex = article.get("openalex") or {}
        sample_articles.append(
            {
                "title": article.get("title"),
                "doi": article.get("doi"),
                "publication_year": article.get("publication_year"),
                "volume": article.get("volume"),
                "issue": article.get("issue"),
                "citation_count_from_openalex": openalex.get("cited_by_count"),
                "reference_count": openalex.get("referenced_works_count"),
                "authors_count": len(article.get("authors", []) or []),
                "keywords_count": len(article.get("keywords", []) or []),
                "has_abstract": bool(article.get("abstract")),
                "has_pdf_url": bool(article.get("pdf_url")),
            }
        )

    return {
        "source_file": str(DEFAULT_INPUT),
        "mode": "DRY_RUN_PREVIEW_ONLY",
        "journal_mapping": {
            "Publisher.display_name_from_publisher": journal.get("publisher") or journal.get("university"),
            "Zone.country_id": 81,
            "Zone.country_name": "Viet Nam",
            "Journal.source_id_from_base_url": journal.get("base_url"),
            "Journal.display_name": journal.get("name_en") or journal.get("name_vi"),
            "Journal.type": str(journal.get("type") or "journal").lower(),
            "Journal.coverage": journal.get("coverage"),
            "Journal.issn": normalize_issn(journal),
        },
        "planned_db_operations": {
            "Publisher_upsert": 1 if (journal.get("publisher") or journal.get("university")) else 0,
            "Journal_upsert": 1,
            "Volume_upsert_estimated": len(volume_values),
            "Issue_upsert_estimated": len(issue_values),
            "Article_upsert": len(unique_articles),
            "Author_upsert_estimated": len(set(author_keys)),
            "Author_Article_link_estimated": article_author_links,
            "authorship_institution_count": authorship_institution_count,
            "Institution_upsert_estimated": len(institution_keys),
            "Institution_Author_link_estimated": institution_author_links,
            "Keyword_upsert_estimated": len(set(keyword_values)),
            "Keyword_Article_link_estimated": keyword_article_links,
            "Article_Citing_Work_upsert_estimated": citing_work_links,
            "Article_Reference_upsert_estimated": reference_links,
        },
        "data_quality": {
            "raw_articles": len(articles),
            "unique_articles_by_doi_or_title": len(unique_articles),
            "duplicate_articles_skipped_estimated": duplicate_article_count,
            "articles_with_abstract": articles_with_abstract,
            "articles_with_pdf_url": articles_with_pdf,
            "articles_with_openalex_block": articles_with_openalex,
            "articles_with_openalex_oa_true": oa_true,
            "articles_with_openalex_oa_false": oa_false,
            "articles_with_oa_unavailable": oa_unavailable,
            "articles_with_citation_history": articles_with_citation_history,
            "articles_without_citation_history": articles_without_citation_history,
            "publication_year_distribution_top10": dict(year_counter.most_common(10)),
        },
        "sample_articles": sample_articles,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview full VN journal import before Supabase execution")
    parser.add_argument("--json-file", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--examples", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="Limit number of articles to preview")
    args = parser.parse_args()

    package = load_package(args.json_file)
    preview = build_preview(package, args.examples, args.limit)
    preview["source_file"] = str(args.json_file)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    print("\n[DRY RUN] Preview only. No Supabase changes were made.")


if __name__ == "__main__":
    main()
