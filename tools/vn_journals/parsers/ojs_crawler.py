from __future__ import annotations

import html
import re
from urllib.parse import urljoin

import requests

from ..base_crawler import BaseJournalCrawler
from ..models import CrawledArticle, CrawlResult, JournalSeed

_LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\'][^>]*>',
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_YEAR_RE = re.compile(r"\((20\d{2}|19\d{2})\)|\b(20\d{2}|19\d{2})\b")


def _clean_text(value: str) -> str:
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _links(page_html: str, base_url: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for href, label_html in _LINK_RE.findall(page_html):
        url = urljoin(base_url, html.unescape(href))
        label = _clean_text(label_html)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append((url, label))
    return out


class OjsCrawler(BaseJournalCrawler):
    """MVP crawler for OJS-based Vietnamese journal sites."""

    platform_name = "ojs"
    timeout_seconds = 30

    def can_handle(self, seed: JournalSeed) -> bool:
        return seed.platform.lower() == self.platform_name

    def crawl(self, seed: JournalSeed, limit: int | None = None) -> CrawlResult:
        warnings: list[str] = []
        archive_url = self._archive_url(seed)
        archive_html = self._fetch(archive_url)
        issue_links = self._extract_issue_links(archive_html, archive_url)
        if not issue_links:
            warnings.append(f"Không tìm thấy issue trong archive: {archive_url}")
            return CrawlResult(journal=seed, articles=[], warnings=warnings)

        articles: list[CrawledArticle] = []
        for issue_url, issue_label in issue_links:
            issue_html = self._fetch(issue_url)
            for article_url, title, pdf_url in self._extract_articles(issue_html, issue_url):
                detail = self._parse_article_detail(article_url)
                articles.append(
                    CrawledArticle(
                        source_journal_code=seed.code,
                        source_url=article_url,
                        title=detail.get("title") or title,
                        authors=detail.get("authors", []),
                        abstract=detail.get("abstract"),
                        keywords=detail.get("keywords", []),
                        doi=detail.get("doi"),
                        pdf_url=detail.get("pdf_url") or pdf_url,
                        publication_year=detail.get("publication_year") or self._year_from_text(issue_label),
                        volume=issue_label,
                        issue=issue_label,
                        language=seed.language,
                        raw={"issue_url": issue_url, "issue_label": issue_label},
                    )
                )
                if limit and len(articles) >= limit:
                    return CrawlResult(journal=seed, articles=articles, warnings=warnings)

        return CrawlResult(journal=seed, articles=articles, warnings=warnings)

    def _archive_url(self, seed: JournalSeed) -> str:
        archive_url = getattr(seed, "archive_url", None)
        if archive_url:
            return archive_url
        base = seed.base_url.rstrip("/") + "/"
        return urljoin(base, "issue/archive")

    def _fetch(self, url: str) -> str:
        headers = {"User-Agent": "Mozilla/5.0 VNJournalCrawler/0.1"}
        response = requests.get(url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    def _extract_issue_links(self, page_html: str, page_url: str) -> list[tuple[str, str]]:
        issue_links: list[tuple[str, str]] = []
        for url, label in _links(page_html, page_url):
            if "/issue/view/" not in url:
                continue
            if not label or label.lower() == "pdf":
                continue
            issue_links.append((url, label))
        return issue_links

    def _extract_articles(self, issue_html: str, issue_url: str) -> list[tuple[str, str, str | None]]:
        article_rows: list[tuple[str, str, str | None]] = []
        last_article: tuple[str, str] | None = None
        seen: set[str] = set()

        for url, label in _links(issue_html, issue_url):
            if "/article/view/" not in url:
                continue
            if label.lower() == "pdf":
                if last_article:
                    article_url, title = last_article
                    if article_url not in seen:
                        seen.add(article_url)
                        article_rows.append((article_url, title, url))
                continue
            last_article = (url, label)
            if url not in seen:
                seen.add(url)
                article_rows.append((url, label, None))

        return article_rows

    def _parse_article_detail(self, article_url: str) -> dict[str, object]:
        page = self._fetch(article_url)
        meta: dict[str, list[str]] = {}
        for name, content in _META_RE.findall(page):
            key = name.strip().lower()
            meta.setdefault(key, []).append(_clean_text(content))

        title = self._first(meta, "citation_title", "dc.title", "og:title")
        authors = meta.get("citation_author", []) or meta.get("dc.creator", [])
        abstract = self._first(meta, "citation_abstract", "dc.description", "description")
        doi = self._first(meta, "citation_doi", "dc.identifier")
        if doi and doi.lower().startswith("doi:"):
            doi = doi[4:].strip()
        pdf_url = self._first(meta, "citation_pdf_url")
        year = self._year_from_text(self._first(meta, "citation_publication_date", "dc.date") or "")
        keywords = []
        for key in ("citation_keywords", "dc.subject", "keywords"):
            for value in meta.get(key, []):
                keywords.extend(part.strip() for part in re.split(r"[;,]", value) if part.strip())

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "keywords": list(dict.fromkeys(keywords)),
            "doi": doi,
            "pdf_url": pdf_url,
            "publication_year": year,
        }

    def _first(self, meta: dict[str, list[str]], *keys: str) -> str | None:
        for key in keys:
            values = meta.get(key.lower(), [])
            if values:
                return values[0]
        return None

    def _year_from_text(self, text: str) -> int | None:
        match = _YEAR_RE.search(text or "")
        if not match:
            return None
        return int(next(group for group in match.groups() if group))
