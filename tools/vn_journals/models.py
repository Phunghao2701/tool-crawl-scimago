from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class JournalSeed:
    """Seed metadata for one Vietnamese university journal source."""

    code: str
    name_vi: str
    name_en: str | None
    university: str
    base_url: str
    archive_url: str | None = None
    platform: str = "unknown"
    issn_print: str | None = None
    issn_online: str | None = None
    language: str = "vi"
    subject_hint: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class CrawledArticle:
    """Normalized article payload produced by a site parser before DB import."""

    source_journal_code: str
    source_url: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    doi: str | None = None
    pdf_url: str | None = None
    publication_year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    language: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CrawlResult:
    """Result returned by a crawler run for one journal source."""

    journal: JournalSeed
    articles: list[CrawledArticle]
    warnings: list[str] = field(default_factory=list)
