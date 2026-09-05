from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests


OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"


TARGET_JOURNALS = [
    {
        "name": "Vietnam Journal of Computer Science",
        "owner": "Nguyen Tat Thanh University",
        "priority": "A_direct_ict",
        "aliases": [
            "Vietnam Journal of Computer Science",
            "VJCS",
        ],
    },
    {
        "name": "Journal of Information and Telecommunication",
        "owner": "Ton Duc Thang University",
        "priority": "A_direct_ict",
        "aliases": [
            "Journal of Information and Telecommunication",
        ],
    },
    {
        "name": "Journal of Computer Science and Cybernetics",
        "owner": "Vietnam Academy of Science and Technology",
        "priority": "A_direct_ict",
        "aliases": [
            "Journal of Computer Science and Cybernetics",
            "Tin học và Điều khiển học",
            "Journal of Computer Science and Cybernertics",
        ],
    },
    {
        "name": "Journal on Information Technologies and Communications",
        "owner": "Ministry of Science and Technology",
        "priority": "A_direct_ict",
        "aliases": [
            "Journal on Information Technologies and Communications",
            "Journal on Information Technologies & Communications",
            "Chuyên san các công trình nghiên cứu phát triển và ứng dụng Công nghệ thông tin và Truyền thông",
        ],
    },
    {
        "name": "EAI Endorsed Transactions on Industrial Networks and Intelligent Systems",
        "owner": "Duy Tan University",
        "priority": "A_direct_ict",
        "aliases": [
            "EAI Endorsed Transactions on Industrial Networks and Intelligent Systems",
            "EAI Endorsed Transaction on Industrial Networks and Intelligent Systems",
            "Industrial Networks and Intelligent Systems",
        ],
    },
    {
        "name": "REV Journal on Electronics and Communications",
        "owner": "Radio and Electronics Association of Vietnam",
        "priority": "A_near_ict",
        "aliases": [
            "REV Journal on Electronics and Communications",
        ],
    },
    {
        "name": "VNU Journal of Science: Computer Science and Communication Engineering",
        "owner": "Vietnam National University, Hanoi",
        "priority": "B_section_ict",
        "aliases": [
            "VNU Journal of Science: Computer Science and Communication Engineering",
            "Tạp chí Khoa học Đại học Quốc gia Hà Nội: Công nghệ thông tin và Truyền thông",
            "VNU Journal of Science: Computer Science and Communication Engineering",
        ],
    },
    {
        "name": "TNU Journal of Science and Technology",
        "owner": "Thai Nguyen University",
        "priority": "B_section_ict",
        "aliases": [
            "TNU Journal of Science and Technology",
            "Tạp chí Khoa học và Công nghệ Đại học Thái Nguyên",
            "Công nghệ thông tin và Truyền thông",
        ],
    },
    {
        "name": "Hue University Journal of Science: Techniques and Technology",
        "owner": "Hue University",
        "priority": "C_broad_filter_article",
        "aliases": [
            "Hue University Journal of Science: Techniques and Technology",
            "Tạp chí Khoa học Đại học Huế: Kỹ thuật và Công nghệ",
            "Hue University Journal of Science",
        ],
    },
    {
        "name": "The University of Danang Journal of Science and Technology",
        "owner": "The University of Danang",
        "priority": "C_broad_filter_article",
        "aliases": [
            "The University of Danang Journal of Science and Technology",
            "Tạp chí Khoa học và Công nghệ Đại học Đà Nẵng",
            "UD Journal of Science and Technology",
        ],
    },
    {
        "name": "Can Tho University Journal of Science",
        "owner": "Can Tho University",
        "priority": "C_broad_filter_article",
        "aliases": [
            "Can Tho University Journal of Science",
            "CTU Journal of Science",
            "Tạp chí Khoa học Trường Đại học Cần Thơ",
            "CTU Journal of Innovation and Sustainable Development",
        ],
    },
]


@dataclass
class Candidate:
    source: str
    display_name: str | None
    issn_l: str | None
    issn_list: list[str]
    publisher_or_owner: str | None
    openalex_id: str | None
    homepage_url: str | None
    works_count: int | None
    cited_by_count: int | None
    raw_score: float
    confidence: str
    notes: str


@dataclass
class ResolveResult:
    input_name: str
    owner: str | None
    priority: str | None
    best_display_name: str | None
    best_issn_l: str | None
    best_issn_list: str
    best_source: str | None
    confidence: str
    score: float
    openalex_id: str | None
    homepage_url: str | None
    notes: str
    candidates: list[dict[str, Any]]


def normalize_text(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"[^a-z0-9à-ỹđ]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_issn(value: str | None) -> str | None:
    if not value:
        return None
    raw = re.sub(r"[^0-9Xx]", "", value)
    if len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:].upper()}"


def unique_issns(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        issn = normalize_issn(str(value))
        if issn and issn not in seen:
            seen.add(issn)
            result.append(issn)
    return result


def title_similarity(expected_names: list[str], candidate_name: str | None) -> float:
    candidate_norm = normalize_text(candidate_name)
    if not candidate_norm:
        return 0.0
    scores = []
    for expected in expected_names:
        expected_norm = normalize_text(expected)
        if not expected_norm:
            continue
        scores.append(SequenceMatcher(None, expected_norm, candidate_norm).ratio())

        if expected_norm in candidate_norm or candidate_norm in expected_norm:
            scores.append(0.92)
    return max(scores) if scores else 0.0


def owner_bonus(owner: str | None, candidate: dict[str, Any]) -> float:
    if not owner:
        return 0.0

    owner_norm = normalize_text(owner)
    haystack = " ".join(
        normalize_text(str(candidate.get(key) or ""))
        for key in (
            "host_organization_name",
            "publisher",
            "publisher_or_owner",
            "display_name",
        )
    )

    if not owner_norm or not haystack:
        return 0.0

    owner_tokens = [t for t in owner_norm.split() if len(t) >= 4]
    if not owner_tokens:
        return 0.0

    hits = sum(1 for token in owner_tokens if token in haystack)
    return min(0.10, hits / len(owner_tokens) * 0.10)


def confidence_label(score: float, has_issn: bool) -> str:
    if score >= 0.88 and has_issn:
        return "high"
    if score >= 0.75 and has_issn:
        return "medium"
    if score >= 0.60:
        return "low"
    return "manual_review"


def request_json(url: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any] | None:
    response = requests.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            time.sleep(int(retry_after))
            response = requests.get(url, params=params, timeout=timeout)

    if response.status_code >= 400:
        return None

    payload = response.json()
    return payload if isinstance(payload, dict) else None


def openalex_candidates(
    journal: dict[str, Any],
    mailto: str | None,
    api_key: str | None,
    per_page: int,
) -> list[Candidate]:
    names = [journal["name"], *(journal.get("aliases") or [])]
    owner = journal.get("owner")

    all_candidates: list[Candidate] = []

    for query_name in names:
        params: dict[str, Any] = {
            "search": query_name,
            "filter": "type:journal",
            "per-page": per_page,
        }
        if mailto:
            params["mailto"] = mailto
        if api_key:
            params["api_key"] = api_key

        payload = request_json(OPENALEX_SOURCES_URL, params)
        if not payload:
            continue

        for item in payload.get("results", []) or []:
            display_name = item.get("display_name")
            issns = unique_issns(item.get("issn") or [])
            issn_l = normalize_issn(item.get("issn_l"))

            base_score = title_similarity(names, display_name)
            score = base_score + owner_bonus(owner, item)
            if issns or issn_l:
                score += 0.05
            score = min(score, 1.0)

            all_candidates.append(
                Candidate(
                    source="openalex",
                    display_name=display_name,
                    issn_l=issn_l,
                    issn_list=issns,
                    publisher_or_owner=item.get("host_organization_name"),
                    openalex_id=item.get("id"),
                    homepage_url=item.get("homepage_url"),
                    works_count=item.get("works_count"),
                    cited_by_count=item.get("cited_by_count"),
                    raw_score=round(score, 4),
                    confidence=confidence_label(score, bool(issns or issn_l)),
                    notes="OpenAlex source search candidate",
                )
            )

    return dedupe_candidates(all_candidates)


def crossref_candidates(
    journal: dict[str, Any],
    mailto: str | None,
    per_page: int,
) -> list[Candidate]:
    names = [journal["name"], *(journal.get("aliases") or [])]
    owner = journal.get("owner")
    all_candidates: list[Candidate] = []

    for query_name in names:
        params: dict[str, Any] = {
            "query.container-title": query_name,
            "filter": "type:journal-article",
            "rows": per_page,
        }
        if mailto:
            params["mailto"] = mailto

        payload = request_json(CROSSREF_WORKS_URL, params)
        if not payload:
            continue

        items = payload.get("message", {}).get("items", []) or []
        for item in items:
            container_titles = item.get("container-title") or []
            if not container_titles:
                continue

            display_name = str(container_titles[0])
            issns = unique_issns(item.get("ISSN") or [])

            base_score = title_similarity(names, display_name)
            score = base_score + owner_bonus(owner, {"publisher": item.get("publisher")})
            if issns:
                score += 0.05
            score = min(score, 1.0)

            all_candidates.append(
                Candidate(
                    source="crossref",
                    display_name=display_name,
                    issn_l=issns[0] if issns else None,
                    issn_list=issns,
                    publisher_or_owner=item.get("publisher"),
                    openalex_id=None,
                    homepage_url=None,
                    works_count=None,
                    cited_by_count=None,
                    raw_score=round(score, 4),
                    confidence=confidence_label(score, bool(issns)),
                    notes="Crossref work container-title candidate",
                )
            )

    return dedupe_candidates(all_candidates)


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    by_key: dict[tuple[str, str, str], Candidate] = {}

    for cand in candidates:
        key = (
            cand.source,
            normalize_text(cand.display_name),
            "|".join(cand.issn_list),
        )
        existing = by_key.get(key)
        if not existing or cand.raw_score > existing.raw_score:
            by_key[key] = cand

    return sorted(by_key.values(), key=lambda c: c.raw_score, reverse=True)


def resolve_journal(
    journal: dict[str, Any],
    mailto: str | None,
    api_key: str | None,
    per_page: int,
) -> ResolveResult:
    candidates = []
    candidates.extend(openalex_candidates(journal, mailto, api_key, per_page))
    candidates.extend(crossref_candidates(journal, mailto, per_page))
    candidates = dedupe_candidates(candidates)

    best = candidates[0] if candidates else None

    notes = ""
    if not best:
        notes = "No candidate found. Manual search required."
    elif best.confidence in {"low", "manual_review"}:
        notes = "Low-confidence match. Verify against official journal website or ISSN Portal."
    else:
        notes = "Candidate found. Still verify against official journal website before database import."

    return ResolveResult(
        input_name=journal["name"],
        owner=journal.get("owner"),
        priority=journal.get("priority"),
        best_display_name=best.display_name if best else None,
        best_issn_l=best.issn_l if best else None,
        best_issn_list=";".join(best.issn_list) if best else "",
        best_source=best.source if best else None,
        confidence=best.confidence if best else "not_found",
        score=best.raw_score if best else 0.0,
        openalex_id=best.openalex_id if best else None,
        homepage_url=best.homepage_url if best else None,
        notes=notes,
        candidates=[asdict(c) for c in candidates[:10]],
    )


def load_input_csv(path: Path) -> list[dict[str, Any]]:
    journals: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or row.get("journal_name") or "").strip()
            if not name:
                continue
            aliases_raw = row.get("aliases") or ""
            journals.append(
                {
                    "name": name,
                    "owner": (row.get("owner") or row.get("publisher") or "").strip() or None,
                    "priority": (row.get("priority") or "").strip() or None,
                    "aliases": [x.strip() for x in aliases_raw.split(";") if x.strip()],
                }
            )
    return journals


def write_csv(results: list[ResolveResult], path: Path) -> None:
    fields = [
        "input_name",
        "owner",
        "priority",
        "best_display_name",
        "best_issn_l",
        "best_issn_list",
        "best_source",
        "confidence",
        "score",
        "openalex_id",
        "homepage_url",
        "notes",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row.pop("candidates", None)
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve ISSN/ISSN-L for filtered Vietnamese ICT journals using OpenAlex and Crossref."
    )
    parser.add_argument("--input-csv", type=Path, help="Optional CSV with columns: name, owner, priority, aliases")
    parser.add_argument("--output-csv", type=Path, default=Path("data/vietnam_journals/ict_journal_issn_candidates.csv"))
    parser.add_argument("--output-json", type=Path, default=Path("data/vietnam_journals/ict_journal_issn_candidates.json"))
    parser.add_argument("--mailto", default=os.getenv("OPENALEX_EMAIL") or os.getenv("CROSSREF_MAILTO"))
    parser.add_argument("--openalex-api-key", default=os.getenv("OPENALEX_API_KEY"))
    parser.add_argument("--per-page", type=int, default=10)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()

    journals = load_input_csv(args.input_csv) if args.input_csv else TARGET_JOURNALS

    results: list[ResolveResult] = []
    for index, journal in enumerate(journals, 1):
        print(f"[{index}/{len(journals)}] Resolving ISSN: {journal['name']}", flush=True)
        result = resolve_journal(journal, args.mailto, args.openalex_api_key, args.per_page)
        results.append(result)

        print(
            f"  -> {result.best_issn_list or 'NO ISSN'} | "
            f"{result.confidence} | {result.best_display_name or 'not found'}",
            flush=True,
        )
        if args.delay:
            time.sleep(args.delay)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)

    write_csv(results, args.output_csv)
    args.output_json.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"[DONE] CSV:  {args.output_csv}")
    print(f"[DONE] JSON: {args.output_json}")
    print()
    print("Review rows with confidence = low/manual_review before importing to database.")


if __name__ == "__main__":
    main()