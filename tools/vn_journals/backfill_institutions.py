from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.vn_journals.import_one_journal_supabase import get_supabase_url, load_env
from tools.vn_journals.paper_vn_affiliations import (
    insert_institution_author_link,
    normalize_institution_payload,
    normalize_openalex_id,
    resolve_author_id,
    upsert_author_safe,
    upsert_institution,
)
from tools.vn_journals.paper_vn_article_metadata import normalize_doi, normalize_work_id

CHECKPOINT_VERSION = 2
FOUND = "found"
NOT_FOUND = "not_found"
TRANSIENT_FAILURE = "transient_failure"
INVALID_RESPONSE = "invalid_response"
MISSING_IDENTIFIER = "missing_identifier"

SUCCESS = "SUCCESS"
UNAVAILABLE_FROM_SOURCE = "UNAVAILABLE_FROM_SOURCE"
RETRYABLE = "RETRYABLE"
AUTHOR_UNRESOLVED = "AUTHOR_UNRESOLVED"
PLANNED_AUTHOR_REPAIR = "PLANNED_AUTHOR_REPAIR"
ARTICLE_HAS_NO_AUTHORS = "ARTICLE_HAS_NO_AUTHORS"
FAILED = "FAILED"

RETRYABLE_FETCH_OUTCOMES = {TRANSIENT_FAILURE}
FAILED_FETCH_OUTCOMES = {INVALID_RESPONSE, MISSING_IDENTIFIER}


@dataclass
class ArticleOutcome:
    article_id: int
    status: str
    reason: str
    publication_year: int | None = None
    doi: str | None = None
    openalex_id: str | None = None
    authorship_count: int = 0
    authorships_with_institutions: int = 0
    institutions_found: int = 0
    links_inserted: int = 0
    unresolved_authors: list[dict[str, Any]] = field(default_factory=list)
    exact_year_affiliation_exists: bool = False
    planned_author_repairs: list[dict[str, Any]] = field(default_factory=list)
    author_completeness_before: dict[str, int] = field(default_factory=dict)
    author_completeness_after: dict[str, int] = field(default_factory=dict)
    source_unavailable_authorships: list[dict[str, Any]] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        return asdict(self)


def openalex_params(include_api_key: bool = True) -> dict[str, str]:
    params: dict[str, str] = {}
    mailto = os.getenv("OPENALEX_EMAIL")
    api_key = os.getenv("OPENALEX_API_KEY")
    if mailto:
        params["mailto"] = mailto
    if include_api_key and api_key:
        params["api_key"] = api_key
    return params


def work_url_for_identifier(openalex_id: str | None, doi: str | None) -> str | None:
    work_id = normalize_work_id(openalex_id)
    if work_id:
        return f"https://api.openalex.org/works/{work_id.rsplit('/', 1)[-1]}"
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"https://api.openalex.org/works/doi:{quote(normalized_doi, safe='')}"
    return None


def retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def fetch_work_with_outcome(
    openalex_id: str | None,
    doi: str | None,
    max_retries: int = 3,
    session=requests,
) -> tuple[str, dict[str, Any] | None]:
    url = work_url_for_identifier(openalex_id, doi)
    if not url:
        return MISSING_IDENTIFIER, None

    use_api_key = True
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, params=openalex_params(use_api_key), timeout=30)
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= max_retries:
                return TRANSIENT_FAILURE, None
            time.sleep(min(2**attempt, 30))
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return INVALID_RESPONSE, None
            if isinstance(payload, dict):
                return FOUND, payload
            return INVALID_RESPONSE, None
        if response.status_code == 401 and use_api_key:
            use_api_key = False
            continue
        if response.status_code == 404:
            return NOT_FOUND, None
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            if attempt >= max_retries:
                return TRANSIENT_FAILURE, None
            delay = retry_after_seconds(response)
            if delay is None:
                delay = min(2**attempt, 30)
            time.sleep(delay)
            continue
        return INVALID_RESPONSE, None
    return TRANSIENT_FAILURE, None


def fetch_work(openalex_id: str | None, doi: str | None, max_retries: int = 3, session=requests) -> dict[str, Any] | None:
    outcome, payload = fetch_work_with_outcome(openalex_id, doi, max_retries=max_retries, session=session)
    return payload if outcome == FOUND else None


def empty_stats() -> dict[str, int]:
    return {
        "processed": 0,
        "skipped_complete": 0,
        "success": 0,
        "unavailable_from_source": 0,
        "retryable": 0,
        "author_unresolved": 0,
        "article_has_no_authors": 0,
        "failed": 0,
        "missing_identifier": 0,
        "openalex_not_found": 0,
        "openalex_transient_failure": 0,
        "openalex_invalid_response": 0,
        "authors_unresolved": 0,
        "institutions_found": 0,
        "institution_links_inserted": 0,
        "article_still_missing_affiliation": 0,
        "author_repairs_planned": 0,
        "author_repairs_written": 0,
    }


def empty_queues() -> dict[str, list[int]]:
    return {
        "retryable_article_ids": [],
        "unavailable_source_article_ids": [],
        "failed_article_ids": [],
    }


def _merge_unique_ids(existing: list[Any] | None, article_id: int) -> list[int]:
    values = [int(value) for value in (existing or [])]
    if article_id not in values:
        values.append(article_id)
    return values


def load_checkpoint(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if version == 1:
        raise SystemExit(
            "Checkpoint version 1 used last_completed_article_id and cannot safely resume this recovery. "
            "Start a new checkpoint or migrate it manually to version 2 with last_scanned_article_id and queues."
        )
    if version != CHECKPOINT_VERSION:
        raise SystemExit(f"Incompatible checkpoint version: {version}")
    payload = {
        "version": CHECKPOINT_VERSION,
        "last_scanned_article_id": int(data.get("last_scanned_article_id") or 0),
        "stats": empty_stats(),
        **empty_queues(),
    }
    payload["stats"].update({key: int(value) for key, value in (data.get("stats") or {}).items() if key in payload["stats"]})
    for key in empty_queues():
        payload[key] = [int(value) for value in data.get(key, [])]
    return payload


def write_checkpoint(
    path: Path,
    last_scanned_article_id: int,
    stats: dict[str, int],
    queues: dict[str, list[int]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CHECKPOINT_VERSION,
        "last_scanned_article_id": int(last_scanned_article_id),
        "stats": dict(stats),
        **(queues or empty_queues()),
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def write_report(path: Path, outcomes: list[ArticleOutcome], append: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as handle:
            for outcome in outcomes:
                handle.write(json.dumps(outcome.to_report_dict(), ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        return
    existing: list[dict[str, Any]] = []
    if append and path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            existing = loaded
    payload = existing + [outcome.to_report_dict() for outcome in outcomes]
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


def table_exists(conn, table_name: str) -> bool:
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


def article_has_exact_year_affiliation(conn, article_id: int, publication_year: int | None) -> bool:
    if publication_year is None:
        return False
    return bool(
        conn.execute(
            text(
                'SELECT 1 FROM "Author_Article" aa '
                'JOIN "Institution_Author" ia '
                'ON ia."author_id" = aa."author_id" AND ia."year" = :publication_year '
                'WHERE aa."article_id" = :article_id LIMIT 1'
            ),
            {"article_id": article_id, "publication_year": publication_year},
        ).fetchone()
    )


def article_author_completeness(conn, article_id: int, publication_year: int | None) -> dict[str, int]:
    if publication_year is None:
        return {"linked_author_count": 0, "complete_count": 0, "incomplete_count": 0}
    rows = conn.execute(
        text(
            'SELECT aa."author_id", '
            'CASE WHEN EXISTS ('
            'SELECT 1 FROM "Institution_Author" ia '
            'WHERE ia."author_id" = aa."author_id" AND ia."year" = :publication_year'
            ') THEN 1 ELSE 0 END AS "is_complete" '
            'FROM "Author_Article" aa '
            'WHERE aa."article_id" = :article_id'
        ),
        {"article_id": article_id, "publication_year": publication_year},
    ).fetchall()
    complete_count = sum(1 for row in rows if int(row[1]) == 1)
    linked_author_count = len(rows)
    return {
        "linked_author_count": linked_author_count,
        "complete_count": complete_count,
        "incomplete_count": linked_author_count - complete_count,
    }


def select_articles(
    conn,
    last_article_id: int,
    batch_size: int,
    only_missing: bool,
    remaining_limit: int | None,
    incomplete_authorships: bool = False,
    journal_id: int | None = None,
) -> list[Any]:
    limit = batch_size if remaining_limit is None else min(batch_size, remaining_limit)
    if journal_id is not None:
        query = (
            'SELECT a."article_id", a."doi", a."openalex_id", a."publication_year" '
            'FROM "Article" a '
            'JOIN "Issue" i ON a."issue_id" = i."issue_id" '
            'JOIN "Volume" v ON i."volume_id" = v."volume_id" '
            'WHERE a."article_id" > :last_article_id '
            'AND a."publication_year" IS NOT NULL '
            'AND v."journal_id" = :journal_id '
        )
    else:
        query = (
            'SELECT a."article_id", a."doi", a."openalex_id", a."publication_year" '
            'FROM "Article" a '
            'WHERE a."article_id" > :last_article_id '
            'AND a."publication_year" IS NOT NULL '
        )
    if incomplete_authorships:
        query += (
            'AND EXISTS (SELECT 1 FROM "Author_Article" aa '
            'WHERE aa."article_id" = a."article_id") '
            'AND EXISTS ('
            'SELECT 1 FROM "Author_Article" aa_missing '
            'WHERE aa_missing."article_id" = a."article_id" '
            'AND NOT EXISTS ('
            'SELECT 1 FROM "Institution_Author" ia '
            'WHERE ia."author_id" = aa_missing."author_id" '
            'AND ia."year" = a."publication_year")) '
        )
    elif only_missing:
        query += (
            'AND NOT EXISTS ('
            'SELECT 1 FROM "Author_Article" aa '
            'JOIN "Institution_Author" ia '
            'ON ia."author_id" = aa."author_id" AND ia."year" = a."publication_year" '
            'WHERE aa."article_id" = a."article_id") '
        )
    query += 'ORDER BY a."article_id" LIMIT :limit'
    params = {"last_article_id": last_article_id, "limit": limit}
    if journal_id is not None:
        params["journal_id"] = journal_id
    return conn.execute(text(query), params).fetchall()


def select_articles_by_ids(conn, article_ids: list[int], limit: int | None = None) -> list[Any]:
    ids = [int(article_id) for article_id in article_ids]
    if limit is not None:
        ids = ids[:limit]
    if not ids:
        return []
    params = {f"article_id_{index}": article_id for index, article_id in enumerate(ids)}
    placeholders = ", ".join(f":article_id_{index}" for index in range(len(ids)))
    query = (
        'SELECT a."article_id", a."doi", a."openalex_id", a."publication_year" '
        'FROM "Article" a '
        f'WHERE a."article_id" IN ({placeholders}) '
        'AND a."publication_year" IS NOT NULL '
        'ORDER BY a."article_id"'
    )
    return conn.execute(text(query), params).fetchall()


def authorships_to_author_payloads(work: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if not name:
            continue
        payloads.append(
            {
                "name": name,
                "openalex_author_id": author.get("id"),
                "orcid": author.get("orcid"),
                "author_position": authorship.get("author_position"),
                "institutions": authorship.get("institutions") or [],
            }
        )
    return payloads


def _article_authorship_author_ids(conn, article_id: int) -> set[int]:
    rows = conn.execute(
        text('SELECT "author_id" FROM "Author_Article" WHERE "article_id" = :article_id'),
        {"article_id": article_id},
    ).fetchall()
    return {int(row[0]) for row in rows}


def _plan_or_apply_author_repair(conn, article_id: int, authorship: dict[str, Any], dry_run: bool) -> tuple[int | None, dict[str, Any] | None]:
    openalex_author_id = normalize_openalex_id(authorship.get("openalex_author_id"), "author")
    if not openalex_author_id:
        return None, None
    existing_author_id = resolve_author_id(conn, authorship)
    existing_article_author_ids = _article_authorship_author_ids(conn, article_id)
    if existing_author_id is not None and existing_author_id in existing_article_author_ids:
        return int(existing_author_id), None
    planned = {
        "openalex_author_id": openalex_author_id,
        "display_name": authorship.get("name"),
        "author_position": authorship.get("author_position"),
        "action": "link_existing_author" if existing_author_id else "upsert_author_and_link",
    }
    if dry_run:
        return existing_author_id, planned
    author_id = int(existing_author_id) if existing_author_id is not None else upsert_author_safe(conn, authorship)
    conn.execute(
        text(
            'INSERT INTO "Author_Article" ("author_id", "article_id", "author_position") '
            'VALUES (:author_id, :article_id, :author_position) '
            'ON CONFLICT ("author_id", "article_id") DO UPDATE SET '
            '"author_position" = COALESCE(EXCLUDED."author_position", "Author_Article"."author_position")'
        ),
        {"author_id": author_id, "article_id": article_id, "author_position": authorship.get("author_position")},
    )
    return int(author_id), planned


def _classify_source_unavailable(authorship_count: int, institutions_found: int, fetch_outcome: str) -> tuple[str, str]:
    if fetch_outcome == NOT_FOUND:
        return UNAVAILABLE_FROM_SOURCE, "OPENALEX_WORK_NOT_FOUND"
    if authorship_count == 0:
        return ARTICLE_HAS_NO_AUTHORS, "NO_AUTHORSHIPS"
    if institutions_found == 0:
        return UNAVAILABLE_FROM_SOURCE, "NO_AUTHORSHIP_INSTITUTIONS"
    return SUCCESS, "EXACT_YEAR_AFFILIATION_PRESENT"


def process_article(
    conn,
    row: Any,
    stats: dict[str, int],
    dry_run: bool,
    repair_authorships: bool = False,
    incomplete_authorships: bool = False,
) -> ArticleOutcome:
    article_id, doi, openalex_id, publication_year = row
    article_id = int(article_id)
    stats["processed"] += 1
    outcome_base = {
        "article_id": article_id,
        "publication_year": publication_year,
        "doi": normalize_doi(doi),
        "openalex_id": normalize_work_id(openalex_id),
    }
    if not normalize_work_id(openalex_id) and not normalize_doi(doi):
        stats["missing_identifier"] += 1
        stats["failed"] += 1
        stats["article_still_missing_affiliation"] += 1
        return ArticleOutcome(status=FAILED, reason="MISSING_IDENTIFIER", **outcome_base)

    fetch_outcome, work = fetch_work_with_outcome(openalex_id, doi)
    if fetch_outcome == TRANSIENT_FAILURE:
        stats["openalex_transient_failure"] += 1
        stats["retryable"] += 1
        stats["article_still_missing_affiliation"] += 1
        return ArticleOutcome(status=RETRYABLE, reason="OPENALEX_TRANSIENT_FAILURE", **outcome_base)
    if fetch_outcome == INVALID_RESPONSE or not work:
        if fetch_outcome == NOT_FOUND:
            stats["openalex_not_found"] += 1
            stats["unavailable_from_source"] += 1
            stats["article_still_missing_affiliation"] += 1
            status, reason = _classify_source_unavailable(0, 0, fetch_outcome)
            return ArticleOutcome(status=status, reason=reason, **outcome_base)
        stats["openalex_invalid_response"] += 1
        stats["failed"] += 1
        stats["article_still_missing_affiliation"] += 1
        return ArticleOutcome(status=FAILED, reason="OPENALEX_INVALID_RESPONSE", **outcome_base)

    authorships = authorships_to_author_payloads(work)
    report = ArticleOutcome(authorship_count=len(authorships), status=SUCCESS, reason="EXACT_YEAR_AFFILIATION_PRESENT", **outcome_base)
    if incomplete_authorships:
        report.author_completeness_before = article_author_completeness(conn, article_id, publication_year)
    if not authorships:
        stats["article_has_no_authors"] += 1
        stats["article_still_missing_affiliation"] += 1
        report.status, report.reason = _classify_source_unavailable(0, 0, fetch_outcome)
        if incomplete_authorships:
            report.author_completeness_after = report.author_completeness_before
        return report

    for authorship in authorships:
        valid_institutions = [
            institution for institution in authorship.get("institutions") or [] if normalize_institution_payload(institution) is not None
        ]
        if valid_institutions:
            report.authorships_with_institutions += 1
        else:
            report.source_unavailable_authorships.append(
                {
                    "display_name": authorship.get("name"),
                    "openalex_author_id": normalize_openalex_id(authorship.get("openalex_author_id"), "author"),
                    "author_position": authorship.get("author_position"),
                }
            )
        report.institutions_found += len(valid_institutions)

        author_id = resolve_author_id(conn, authorship)
        planned_repair = None
        if repair_authorships:
            repair_author_id, planned_repair = _plan_or_apply_author_repair(conn, article_id, authorship, dry_run)
            if repair_author_id is not None:
                author_id = repair_author_id
            if planned_repair:
                report.planned_author_repairs.append(planned_repair)
                stats["author_repairs_planned"] += 1
                if not dry_run:
                    stats["author_repairs_written"] += 1
        if dry_run and planned_repair:
            continue
        if author_id is None:
            report.unresolved_authors.append(
                {
                    "display_name": authorship.get("name"),
                    "openalex_author_id": normalize_openalex_id(authorship.get("openalex_author_id"), "author"),
                    "author_position": authorship.get("author_position"),
                }
            )
            continue
        if dry_run:
            continue
        for institution in valid_institutions:
            institution_id = upsert_institution(conn, institution)
            if institution_id is None:
                continue
            report.links_inserted += insert_institution_author_link(conn, int(author_id), int(institution_id), int(publication_year))

    stats["authors_unresolved"] += len(report.unresolved_authors)
    stats["institutions_found"] += report.institutions_found
    stats["institution_links_inserted"] += report.links_inserted
    report.exact_year_affiliation_exists = article_has_exact_year_affiliation(conn, article_id, publication_year)
    if incomplete_authorships:
        report.author_completeness_after = article_author_completeness(conn, article_id, publication_year)
    if dry_run and report.institutions_found and not report.unresolved_authors:
        report.exact_year_affiliation_exists = True

    if dry_run and report.planned_author_repairs and not report.unresolved_authors:
        report.status = PLANNED_AUTHOR_REPAIR
        report.reason = "AUTHOR_REPAIR_PLANNED"
    elif report.unresolved_authors:
        stats["author_unresolved"] += 1
        stats["article_still_missing_affiliation"] += 1
        report.status = AUTHOR_UNRESOLVED
        report.reason = "AUTHOR_IDENTITY_UNRESOLVED"
    elif incomplete_authorships and report.author_completeness_after.get("incomplete_count", 0) > 0:
        stats["unavailable_from_source"] += 1
        stats["article_still_missing_affiliation"] += 1
        report.status = UNAVAILABLE_FROM_SOURCE
        report.reason = "INCOMPLETE_AUTHORSHIPS_SOURCE_UNAVAILABLE"
    elif report.institutions_found == 0:
        stats["unavailable_from_source"] += 1
        stats["article_still_missing_affiliation"] += 1
        report.status, report.reason = _classify_source_unavailable(len(authorships), report.institutions_found, fetch_outcome)
    elif report.exact_year_affiliation_exists:
        stats["success"] += 1
        report.status = SUCCESS
        report.reason = "EXACT_YEAR_AFFILIATION_PRESENT"
    else:
        stats["failed"] += 1
        stats["article_still_missing_affiliation"] += 1
        report.status = FAILED
        report.reason = "NO_EXACT_YEAR_AFFILIATION_AFTER_PROCESSING"
    return report


def _update_checkpoint_queues(queues: dict[str, list[int]], outcome: ArticleOutcome) -> None:
    for key in empty_queues():
        queues[key] = [int(value) for value in queues.get(key, []) if int(value) != outcome.article_id]
    if outcome.status == RETRYABLE:
        queues["retryable_article_ids"] = _merge_unique_ids(queues.get("retryable_article_ids"), outcome.article_id)
    elif outcome.status in {UNAVAILABLE_FROM_SOURCE, ARTICLE_HAS_NO_AUTHORS}:
        queues["unavailable_source_article_ids"] = _merge_unique_ids(queues.get("unavailable_source_article_ids"), outcome.article_id)
    elif outcome.status in {FAILED, AUTHOR_UNRESOLVED}:
        queues["failed_article_ids"] = _merge_unique_ids(queues.get("failed_article_ids"), outcome.article_id)


def _process_article_rows(
    engine,
    rows: list[Any],
    args: argparse.Namespace,
    stats: dict[str, int],
    queues: dict[str, list[int]],
    last_article_id: int,
    remaining_limit: int | None,
    advance_scanned_id: bool,
) -> tuple[int, int | None, bool]:
    reports: list[ArticleOutcome] = []
    stop_after_retryable = False
    for row in rows:
        article_id = int(row[0])
        if args.only_missing and not args.incomplete_authorships:
            with engine.connect() as conn:
                if article_has_exact_year_affiliation(conn, article_id, row[3]):
                    stats["skipped_complete"] += 1
                    if advance_scanned_id:
                        last_article_id = article_id
                    if args.checkpoint and not args.dry_run:
                        write_checkpoint(args.checkpoint, last_article_id, stats, queues)
                    continue
        if args.dry_run:
            with engine.connect() as conn:
                outcome = process_article(conn, row, stats, True, args.repair_authorships, args.incomplete_authorships)
        else:
            with engine.begin() as conn:
                outcome = process_article(conn, row, stats, False, args.repair_authorships, args.incomplete_authorships)
        reports.append(outcome)
        _update_checkpoint_queues(queues, outcome)
        if outcome.status != RETRYABLE and advance_scanned_id:
            last_article_id = article_id
        if args.checkpoint and not args.dry_run:
            write_checkpoint(args.checkpoint, last_article_id, stats, queues)
        if args.sleep:
            time.sleep(args.sleep)
        if remaining_limit is not None:
            remaining_limit -= 1
            if remaining_limit <= 0:
                stop_after_retryable = outcome.status == RETRYABLE
                break
        if outcome.status == RETRYABLE:
            stop_after_retryable = True
            break
    if args.report and reports:
        write_report(args.report, reports, append=True)
    return last_article_id, remaining_limit, stop_after_retryable


def run_backfill(engine, args: argparse.Namespace) -> dict[str, int]:
    stats = empty_stats()
    queues = empty_queues()
    last_article_id = 0
    if args.resume:
        if not args.checkpoint or not args.checkpoint.exists():
            raise SystemExit("--resume requires an existing --checkpoint file")
        checkpoint = load_checkpoint(args.checkpoint)
        last_article_id = int(checkpoint.get("last_scanned_article_id") or 0)
        stats.update({key: int(value) for key, value in (checkpoint.get("stats") or {}).items() if key in stats})
        for key in queues:
            queues[key] = [int(value) for value in checkpoint.get(key, [])]

    # Resolve journal_id if journal_code is provided
    journal_id = None
    if getattr(args, "journal_code", None):
        registry_path = getattr(args, "registry", None) or (REPO_ROOT / "data" / "vietnam_journals" / "vn_journals_registry.json")
        if not registry_path.exists():
            raise SystemExit(f"Registry file not found at {registry_path}")
        with registry_path.open(encoding="utf-8") as f:
            registry = json.load(f)
        if args.journal_code not in registry:
            raise SystemExit(f"Journal code '{args.journal_code}' not found in registry")
        
        journal_entry = registry[args.journal_code]
        base_url = journal_entry.get("base_url")
        display_name = journal_entry.get("name_en") or journal_entry.get("name_vi")
        
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
                raise SystemExit(f"Journal '{display_name}' not found in database.")
            journal_id = int(journal_row[0])

    with engine.connect() as conn:
        if not table_exists(conn, "Institution") or not table_exists(conn, "Institution_Author"):
            raise SystemExit("Institution tables are missing; run migrations explicitly before backfill.")

    remaining_limit = args.limit
    if args.resume and queues["retryable_article_ids"] and (remaining_limit is None or remaining_limit > 0):
        while queues["retryable_article_ids"] and (remaining_limit is None or remaining_limit > 0):
            with engine.connect() as conn:
                rows = select_articles_by_ids(conn, queues["retryable_article_ids"], remaining_limit)
            if not rows:
                break
            last_article_id, remaining_limit, stop_after_retryable = _process_article_rows(
                engine,
                rows,
                args,
                stats,
                queues,
                last_article_id,
                remaining_limit,
                advance_scanned_id=True,
            )
            if stop_after_retryable:
                return stats

    while remaining_limit is None or remaining_limit > 0:
        with engine.connect() as conn:
            rows = select_articles(
                conn,
                last_article_id,
                args.batch_size,
                args.only_missing,
                remaining_limit,
                args.incomplete_authorships,
                journal_id,
            )
        if not rows:
            break
        last_article_id, remaining_limit, stop_after_retryable = _process_article_rows(
            engine,
            rows,
            args,
            stats,
            queues,
            last_article_id,
            remaining_limit,
            advance_scanned_id=True,
        )
        if stop_after_retryable:
            break
    return stats


def print_stats(stats: dict[str, int]) -> None:
    labels = [
        ("processed", "processed"),
        ("skipped_complete", "skipped complete"),
        ("success", "success"),
        ("unavailable_from_source", "unavailable from source"),
        ("retryable", "retryable"),
        ("author_unresolved", "author unresolved articles"),
        ("article_has_no_authors", "article has no authors"),
        ("failed", "failed"),
        ("missing_identifier", "missing identifier"),
        ("openalex_not_found", "OpenAlex not found"),
        ("openalex_transient_failure", "OpenAlex transient failures"),
        ("openalex_invalid_response", "OpenAlex invalid responses"),
        ("authors_unresolved", "authors unresolved"),
        ("institutions_found", "institutions found"),
        ("institution_links_inserted", "institution links inserted"),
        ("article_still_missing_affiliation", "article still missing affiliation"),
        ("author_repairs_planned", "author repairs planned"),
        ("author_repairs_written", "author repairs written"),
    ]
    for key, label in labels:
        print(f"{label}: {stats[key]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely backfill PaperVN historical Institution_Author links")
    parser.add_argument("--only-missing", action="store_true", help="Only process articles missing any exact-publication-year affiliation")
    parser.add_argument(
        "--incomplete-authorships",
        action="store_true",
        help="Process articles where at least one Author_Article lacks exact-year Institution_Author",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Stable article_id batch size")
    parser.add_argument("--checkpoint", type=Path, help="Version 2 JSON checkpoint path with last_scanned_article_id and queues")
    parser.add_argument("--resume", action="store_true", help="Continue after last_scanned_article_id from a checkpoint v2 file")
    parser.add_argument("--report", type=Path, help="Write per-article JSON or JSONL outcome report without secrets")
    parser.add_argument("--repair-authorships", action="store_true", help="Opt in to missing Author/Author_Article repair from OpenAlex author IDs")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report only; do not write Author, Institution or link rows")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of selected articles to process across batches")
    parser.add_argument("--sleep", type=float, default=0.0, help="Optional delay between OpenAlex requests")
    parser.add_argument("--journal-code", type=str, help="Target journal code in registry")
    parser.add_argument(
        "--registry",
        type=Path,
        default=REPO_ROOT / "data" / "vietnam_journals" / "vn_journals_registry.json",
        help="Path to registry file",
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    if args.only_missing and args.incomplete_authorships:
        raise SystemExit("--only-missing and --incomplete-authorships are separate selection modes")

    load_env()
    engine = create_engine(get_supabase_url())
    stats = run_backfill(engine, args)
    print_stats(stats)


if __name__ == "__main__":
    main()
