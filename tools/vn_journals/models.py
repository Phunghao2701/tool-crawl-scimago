from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class JournalSeed:
    """Seed metadata for one Vietnamese journal source."""

    code: str
    name_vi: str
    name_en: str | None
    university: str
    base_url: str
    archive_url: str | None = None
    platform: str = "unknown"
    issn_print: str | None = None
    issn_online: str | None = None
    language: str | None = None
    subject_hint: str | None = None
    publisher: str | None = None
    owning_institution: str | None = None
    coverage: str | None = None
    country: str | None = None
    type: str | None = None
    notes: str | None = None


@dataclass
class CrawledArticle:
    """Normalized article payload before database import."""

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


@dataclass
class CrawlResult:
    journal: JournalSeed
    articles: list[CrawledArticle]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal": asdict(self.journal),
            "articles": [asdict(article) for article in self.articles],
            "warnings": self.warnings,
        }
