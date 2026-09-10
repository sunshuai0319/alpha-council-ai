import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from app.collectors.base import CollectorResult


@dataclass(frozen=True)
class NewsItem:
    source: str
    canonical_url: str
    title: str
    summary: str
    content_hash: str
    published_at: datetime | None
    fetched_at: datetime


RSS_SOURCES = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "bitcoin-magazine": "https://bitcoinmagazine.com/.rss/full/",
}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def _entry_summary(entry: Any) -> str:
    content = entry.get("content") or []
    if content and isinstance(content[0], dict):
        return _clean_text(str(content[0].get("value", "")))
    return _clean_text(str(entry.get("summary") or entry.get("description") or ""))


class RSSCollector:
    """Fetch and normalize free RSS feeds with URL/content-hash de-duplication."""

    def __init__(
        self,
        sources: dict[str, str] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        parser: Callable[[Any], Any] | None = None,
    ) -> None:
        self.sources = sources or RSS_SOURCES
        self._fetcher = fetcher or self._fetch
        self._parser = parser or feedparser.parse

    @staticmethod
    def _fetch(url: str) -> bytes:
        response = httpx.get(
            url,
            headers={"User-Agent": "alpha-council-ai/0.1 (+https://example.invalid)"},
            timeout=20,
        )
        response.raise_for_status()
        return response.content

    def collect(self, urls: dict[str, str] | None = None) -> CollectorResult[NewsItem]:
        result: CollectorResult[NewsItem] = CollectorResult(source="rss")
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        for source, url in (urls or self.sources).items():
            try:
                feed = self._parser(self._fetcher(url))
                for entry in getattr(feed, "entries", []) or []:
                    title = _clean_text(str(entry.get("title", "")))
                    summary = _entry_summary(entry)
                    entry_url = canonicalize_url(str(entry.get("link") or entry.get("id") or ""))
                    if not entry_url or not title:
                        continue
                    content_hash = hashlib.sha256(f"{title}\n{summary}".encode()).hexdigest()
                    if entry_url in seen_urls or content_hash in seen_hashes:
                        continue
                    seen_urls.add(entry_url)
                    seen_hashes.add(content_hash)
                    result.items.append(
                        NewsItem(
                            source=source,
                            canonical_url=entry_url,
                            title=title,
                            summary=summary,
                            content_hash=content_hash,
                            published_at=_published_at(entry),
                            fetched_at=result.collected_at,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - a broken feed is non-fatal
                result.errors.append(f"{source}: {exc}")
        return result
