from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.models import CrawledArticle, CrawlResult, JournalSeed

DEFAULT_OUTPUT = Path("E:/tool-crawl-scimago/data/vietnam_journals/lens_preview.json")
DEFAULT_REGISTRY = REPO_ROOT / "data" / "vietnam_journals" / "vn_journals_registry.json"

ALIASES = {
    "title": ["title", "work title", "scholarly work", "publication title"],
    "doi": ["doi"],
    "abstract": ["abstract"],
    "authors": ["authors", "author", "author names", "author/s"],
    "keywords": ["keywords", "author keywords"],
    "issn": ["issn", "issns", "journal issn", "source issn"],
    "source_url": ["source url", "source urls", "lens url", "url", "scholarly works url", "external url"],
    "pdf_url": ["pdf url", "full text url", "open access url"],
    "publication_year": ["publication year", "year", "published year", "date published"],
    "volume": ["volume"],
    "issue": ["issue", "issue number"],
    "pages": ["pages", "page", "page range"],
    "start_page": ["start page"],
    "end_page": ["end page"],
    "language": ["language"],
    "publisher": ["publisher", "source publisher"],
    "journal_name": ["source title", "journal title", "journal", "source"],
    "affiliations": ["affiliations", "author affiliations"],
    "open_access": ["open access", "is open access", "oa status"],
    "open_access_license": ["open access license"],
    "open_access_colour": ["open access colour", "open access color"],
    "patent_citations": ["patent citations", "citing patents count", "scholarly citations by patent count"],
    "scholarly_citations": ["scholarly citations", "citing works count", "times cited", "scholarly citation count"],
    "lens_id": ["lens id"],
    "fields_of_study": ["fields of study"],
    "external_url": ["external url"],
}


def normalize_header(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def pick_value(row: dict[str, Any], field_name: str) -> Any:
    for alias in ALIASES[field_name]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def split_multi(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"\s*[;|]\s*|\s*,\s*(?=[A-ZÀ-Ỹ])", text)
    # Xóa tiền tố "null " bị sinh ra do lỗi của Lens export trong một số trường hợp
    return [re.sub(r"^null\s+", "", p.strip(), flags=re.IGNORECASE) for p in parts if p.strip()]

def strip_jats_tags(text: str | None) -> str | None:
    if not text:
        return None
    # Xóa các tag XML/HTML thường gặp trong Lens export
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip() or None

def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    match = re.search(r"(19|20)\d{2}", text)
    return int(match.group(0)) if match else None


def load_table(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            return [{normalize_header(k): v for k, v in row.items() if k} for row in reader]

    if suffix in {".xlsx", ".xls"}:
        frame = pd.read_excel(path)
        frame.columns = [normalize_header(str(col)) for col in frame.columns]
        return frame.fillna("").to_dict(orient="records")

    raise ValueError(f"Unsupported file format: {path.suffix}")


def load_journal_registry(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Journal registry must be a JSON object: {path}")
    return {str(code): meta for code, meta in data.items() if isinstance(meta, dict)}


def apply_registry(seed: JournalSeed, registry: dict[str, dict[str, Any]]) -> JournalSeed:
    meta = registry.get(seed.code)
    if not meta:
        return seed

    allowed_fields = set(JournalSeed.__dataclass_fields__)
    updated = seed.__dict__.copy()
    lens_publisher = seed.university

    for key, value in meta.items():
        if key in allowed_fields and value not in (None, ""):
            updated[key] = value

    notes = str(updated.get("notes") or "").strip()
    registry_note = "Metadata overlaid from VN journal registry."
    if registry_note not in notes:
        notes = f"{notes} | {registry_note}" if notes else registry_note
    publisher = meta.get("publisher")
    if publisher and "Publisher set from VN journal registry." not in notes:
        notes = f"{notes} | Publisher set from VN journal registry."
    if lens_publisher and lens_publisher != updated.get("university") and f"Lens publisher: {lens_publisher}" not in notes:
        notes = f"{notes} | Lens publisher: {lens_publisher}"
    updated["notes"] = notes

    return JournalSeed(**{key: updated[key] for key in allowed_fields})


def infer_seed(rows: list[dict[str, Any]], input_path: Path, journal_code: str | None, registry: dict[str, dict[str, Any]] | None = None) -> JournalSeed:
    sample = rows[0] if rows else {}
    journal_name = pick_value(sample, "journal_name") or input_path.stem
    issn = pick_value(sample, "issn")
    publisher = pick_value(sample, "publisher") or "Unknown"
    seed = JournalSeed(
        code=journal_code or re.sub(r"[^a-z0-9]+", "_", input_path.stem.lower()).strip("_"),
        name_vi=str(journal_name),
        name_en=str(journal_name),
        university=str(publisher),
        base_url="https://about.lens.org/",
        platform="lens_export",
        issn_print=str(issn) if issn else None,
        language="vi,en",
        subject_hint="Lens export",
        publisher=str(publisher),
        notes=f"Imported from Lens export: {input_path.name}",
    )
    return apply_registry(seed, registry or {})


def map_articles(rows: list[dict[str, Any]], seed: JournalSeed) -> list[CrawledArticle]:
    articles: list[CrawledArticle] = []
    for row in rows:
        title = pick_value(row, "title")
        if not title:
            continue
        pages = pick_value(row, "pages")
        if not pages:
            start_page = pick_value(row, "start_page")
            end_page = pick_value(row, "end_page")
            if start_page and end_page:
                pages = f"{start_page}-{end_page}"
            elif start_page:
                pages = start_page

        # Bóc tách source_url và pdf_url thông minh từ các cột URL của Lens
        source_urls_raw = str(pick_value(row, "source_url") or "").strip()
        ext_url = str(pick_value(row, "external_url") or "").strip()
        
        all_candidate_urls = []
        if source_urls_raw:
            all_candidate_urls.extend(source_urls_raw.split())
        if ext_url and ext_url not in all_candidate_urls:
            all_candidate_urls.append(ext_url)
            
        source_url = ""
        pdf_url_from_lens = pick_value(row, "pdf_url")
        pdf_url = str(pdf_url_from_lens).strip() if pdf_url_from_lens else None
        
        for u in all_candidate_urls:
            u_lower = u.lower()
            if "/download/" in u_lower or u_lower.endswith(".pdf"):
                if not pdf_url:
                    pdf_url = u
            else:
                if not source_url:
                    source_url = u
                    
        # Nếu vẫn chưa tìm thấy source_url riêng biệt, lấy URL đầu tiên làm default
        if not source_url and all_candidate_urls:
            source_url = all_candidate_urls[0]

        # Xử lý abstract và strip tag JATS
        abstract_raw = pick_value(row, "abstract")
        abstract = strip_jats_tags(str(abstract_raw)) if abstract_raw else None

        # Xử lý keywords
        keywords = split_multi(pick_value(row, "keywords"))
        if not keywords:
            # Fallback lấy từ fields of study nếu keywords của Lens bị trống
            fields = pick_value(row, "fields_of_study")
            if fields:
                keywords = [f.strip() for f in str(fields).split(";") if f.strip()]

        article = CrawledArticle(
            source_journal_code=seed.code,
            source_url=source_url,
            title=str(title).strip(),
            authors=split_multi(pick_value(row, "authors")),
            abstract=abstract,
            keywords=keywords,
            doi=(str(pick_value(row, "doi")).strip() or None) if pick_value(row, "doi") else None,
            pdf_url=pdf_url,
            publication_year=to_int(pick_value(row, "publication_year")),
            volume=(str(pick_value(row, "volume")).strip() or None) if pick_value(row, "volume") else None,
            issue=(str(pick_value(row, "issue")).strip() or None) if pick_value(row, "issue") else None,
            pages=(str(pages).strip() or None) if pages else None,
            language=(str(pick_value(row, "language")).strip() or seed.language) if pick_value(row, "language") else seed.language,
            raw={
                "lens_id": pick_value(row, "lens_id"),
                "lens_affiliations": pick_value(row, "affiliations"),
                "lens_open_access": pick_value(row, "open_access"),
                "lens_open_access_license": pick_value(row, "open_access_license"),
                "lens_open_access_colour": pick_value(row, "open_access_colour"),
                "lens_patent_citations": pick_value(row, "patent_citations"),
                "lens_scholarly_citations": pick_value(row, "scholarly_citations"),
                "lens_fields_of_study": pick_value(row, "fields_of_study"),
                "lens_row": row,
            },
        )
        articles.append(article)
    return articles


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize Lens CSV/XLSX export into vn_journals preview JSON")
    parser.add_argument("input_file", help="Path to Lens export CSV/XLSX")
    parser.add_argument("--journal-code", help="Override journal code")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON path")
    parser.add_argument(
        "--journal-registry",
        default=str(DEFAULT_REGISTRY),
        help="Path to curated VN journal metadata registry JSON",
    )
    args = parser.parse_args()

    input_path = Path(args.input_file)
    registry = load_journal_registry(Path(args.journal_registry))
    rows = load_table(input_path)
    seed = infer_seed(rows, input_path, args.journal_code, registry)
    result = CrawlResult(journal=seed, articles=map_articles(rows, seed), warnings=[])

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([result.to_dict()], ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Rows read: {len(rows)}")
    print(f"Articles mapped: {len(result.articles)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
