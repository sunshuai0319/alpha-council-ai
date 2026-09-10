import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.collectors.rss import canonicalize_url
from app.db.models import SourceDocument

ASSET_PATTERNS = {
    "BTC": r"(?i)(?<![A-Z])(?:BTC|BITCOIN)(?![A-Z])",
    "ETH": r"(?i)(?<![A-Z])(?:ETH|ETHEREUM)(?![A-Z])",
    "SOL": r"(?i)(?<![A-Z])(?:SOL|SOLANA)(?![A-Z])",
}
EVENT_PATTERNS = (
    ("ETF", r"(?i)\betf\b"),
    ("FOMC", r"(?i)\bfomc\b|federal reserve|fed meeting"),
    ("INFLATION", r"(?i)\bcpi\b|inflation"),
    ("EMPLOYMENT", r"(?i)\bunemployment\b|\bjobs report\b|nonfarm payroll"),
    ("REGULATION", r"(?i)regulat(?:ion|ory)|sec|legislation"),
)


@dataclass(frozen=True)
class DocumentInput:
    url: str
    title: str
    content: str
    source: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class ProcessedDocument:
    document_id: str
    source: str
    canonical_url: str
    title: str
    raw_text: str
    cleaned_text: str
    content_hash: str
    language: str
    assets: tuple[str, ...]
    event_type: str
    published_at: datetime | None = None

    @property
    def id(self) -> str:
        """Compatibility alias with the SQLAlchemy SourceDocument primary key."""

        return self.document_id


def clean_text(text: str) -> str:
    without_scripts = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", without_tags).strip()


def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    return "zh" if chinese >= max(2, letters * 0.1) else "en"


def detect_assets(text: str) -> tuple[str, ...]:
    return tuple(asset for asset, pattern in ASSET_PATTERNS.items() if re.search(pattern, text))


def detect_event_type(text: str) -> str:
    for event_type, pattern in EVENT_PATTERNS:
        if re.search(pattern, text):
            return event_type
    return "MARKET"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocumentRepository:
    """Idempotent document persistence, usable with a SQLAlchemy session or in memory."""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db
        self._records: dict[str, ProcessedDocument] = {}

    def upsert(
        self,
        url: str,
        content: str,
        *,
        source: str = "unknown",
        title: str = "",
        published_at: datetime | None = None,
    ) -> ProcessedDocument | SourceDocument:
        canonical_url = canonicalize_url(url)
        digest = content_hash(content)
        if self.db is None:
            existing = next(
                (
                    record
                    for record in self._records.values()
                    if record.canonical_url == canonical_url or record.content_hash == digest
                ),
                None,
            )
            if existing:
                return existing
            record = ProcessedDocument(
                document_id=str(uuid4()),
                source=source,
                canonical_url=canonical_url,
                title=title,
                raw_text=content,
                cleaned_text=clean_text(content),
                content_hash=digest,
                language=detect_language(content),
                assets=detect_assets(f"{title}\n{content}"),
                event_type=detect_event_type(f"{title}\n{content}"),
                published_at=published_at,
            )
            self._records[record.document_id] = record
            return record

        existing = self.db.scalar(
            select(SourceDocument).where(
                or_(SourceDocument.canonical_url == canonical_url, SourceDocument.content_hash == digest)
            )
        )
        if existing:
            return existing
        record = SourceDocument(
            id=str(uuid4()),
            source=source,
            canonical_url=canonical_url,
            title=title or canonical_url,
            raw_text=content,
            cleaned_text=clean_text(content),
            content_hash=digest,
            language=detect_language(content),
            published_at=published_at,
        )
        self.db.add(record)
        self.db.flush()
        return record


def prepare_document(document: DocumentInput, document_id: str | None = None) -> ProcessedDocument:
    raw_text = document.content
    cleaned = clean_text(raw_text)
    tagged_text = f"{document.title}\n{cleaned}"
    return ProcessedDocument(
        document_id=document_id or str(uuid4()),
        source=document.source,
        canonical_url=canonicalize_url(document.url),
        title=clean_text(document.title),
        raw_text=raw_text,
        cleaned_text=cleaned,
        content_hash=content_hash(raw_text),
        language=detect_language(tagged_text),
        assets=detect_assets(tagged_text),
        event_type=detect_event_type(tagged_text),
        published_at=document.published_at,
    )


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("chunk overlap must be non-negative and smaller than max_chars")
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        if current and current_length + len(word) + 1 > max_chars:
            chunks.append(" ".join(current))
            overlap_words: list[str] = []
            overlap_length = 0
            for previous in reversed(current):
                if overlap_length + len(previous) + 1 > overlap:
                    break
                overlap_words.append(previous)
                overlap_length += len(previous) + 1
            current = list(reversed(overlap_words))
            current_length = sum(len(item) + 1 for item in current)
        current.append(word)
        current_length += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks
