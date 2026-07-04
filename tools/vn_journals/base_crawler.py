from __future__ import annotations

from abc import ABC, abstractmethod

from .models import CrawlResult, JournalSeed


class BaseJournalCrawler(ABC):
    """Common interface for Vietnamese journal crawlers."""

    platform_name = "base"

    @abstractmethod
    def can_handle(self, seed: JournalSeed) -> bool:
        """Return True when this crawler supports the provided seed."""

    @abstractmethod
    def crawl(self, seed: JournalSeed, limit: int | None = None) -> CrawlResult:
        """Fetch and normalize article data for a journal seed."""
