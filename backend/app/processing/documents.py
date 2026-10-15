import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.collectors.rss import canonicalize_url
from app.db.models import SourceDocument

logger = logging.getLogger(__name__)

ASSET_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "BITCOIN"),
    "ETH": ("ETH", "ETHEREUM"),
    "SOL": ("SOL", "SOLANA"),
    "BCH": ("BCH", "BITCOIN CASH"),
    "LTC": ("LTC", "LITECOIN"),
    "DOGE": ("DOGE", "DOGECOIN"),
    "XRP": ("XRP", "RIPPLE"),
    "ADA": ("ADA", "CARDANO"),
    "AVAX": ("AVAX", "AVALANCHE"),
    "DOT": ("DOT", "POLKADOT"),
    "LINK": ("LINK", "CHAINLINK"),
    "MATIC": ("MATIC", "POLYGON"),
    "XLM": ("XLM", "STELLAR"),
    "ATOM": ("ATOM", "COSMOS"),
    "UNI": ("UNI", "UNISWAP"),
    "AAVE": ("AAVE",),
    "SUI": ("SUI",),
    "TRX": ("TRX", "TRON"),
    "TON": ("TON", "TONCOIN"),
    "BNB": ("BNB", "BINANCE COIN"),
}


def _asset_pattern(aliases: Sequence[str]) -> str:
    terms = [
        f"{re.escape(alias)}[-_]?S?USDT"
        for alias in aliases
        if re.fullmatch(r"[A-Z0-9]+", alias)
    ]
    terms.extend(
        r"BITCOIN(?!\s+CASH)" if alias == "BITCOIN" else re.escape(alias)
        for alias in aliases
    )
    return rf"(?i)(?<![A-Z0-9])(?:{'|'.join(terms)})(?![A-Z0-9])"


# 保持一个公开的规则表，方便离线采集与测试复用；新增品种时只需要扩展别名表。
ASSET_PATTERNS = {asset: _asset_pattern(aliases) for asset, aliases in ASSET_ALIASES.items()}
EVENT_PATTERNS = (
    ("ETF", r"(?i)\betf\b"),
    ("FOMC", r"(?i)\bfomc\b|federal reserve|fed meeting"),
    ("INFLATION", r"(?i)\bcpi\b|inflation"),
    ("EMPLOYMENT", r"(?i)\bunemployment\b|\bjobs report\b|nonfarm payroll"),
    ("REGULATION", r"(?i)regulat(?:ion|ory)|sec|legislation"),
)
MACRO_EVENT_TYPES = {"FOMC", "INFLATION", "EMPLOYMENT", "FED_PRESS", "MACRO"}
UNKNOWN_VALUES = {"", "UNKNOWN", "NONE", "NULL", "N/A", "NA"}
# These values are quote/stablecoin labels rather than tradeable base assets for
# the configured crypto strategy.  LLM summaries often return ``USDT`` merely
# because it appears in a contract symbol; keeping it as an asset creates
# misleading asset-specific RAG rows.
NON_ASSET_CODES = {
    "USD",
    "USDT",
    "USDC",
    "USDS",
    "USDBC",
    "USBDC",
    "FUSD",
    "DAI",
    "TUSD",
    "USDE",
}


def _compact_asset(value: str) -> str:
    return re.sub(r"[\s_/-]+", "", value.strip().upper())


def _base_from_pair(value: str) -> str:
    """Extract the base token from a configured or WEEX contract symbol."""

    normalized = value.strip().upper()
    if "/" in normalized:
        base, quote = normalized.split("/", 1)
        if quote in {"USDT", "USD"} and base:
            return base
    compact = _compact_asset(normalized)
    if compact.endswith("SUSDT"):
        compact = f"{compact[:-5]}USDT"
    if compact.endswith("USDT") and len(compact) > 4:
        return compact[:-4]
    return compact


def _asset_catalog(known_assets: Iterable[str] | None = None) -> dict[str, str]:
    catalog: dict[str, str] = {}
    for canonical, aliases in ASSET_ALIASES.items():
        for alias in (canonical, *aliases):
            catalog[_compact_asset(alias)] = canonical
    for configured in known_assets or ():
        raw = str(configured).strip().upper()
        base = _base_from_pair(raw)
        if base:
            catalog[_compact_asset(raw)] = base
            catalog[_compact_asset(base)] = base
    return catalog


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


def normalize_asset(value: str, known_assets: Iterable[str] | None = None) -> str:
    """Normalize an LLM, article, or WEEX asset label to its base asset.

    The vector store keeps the base token (``BCH``), while callers may provide
    ``Bitcoin Cash``, ``BCH-USDT`` or the virtual-account symbol ``BCHSUSDT``.
    Unknown FX-style pairs are retained in ``BASE/QUOTE`` form instead of being
    silently discarded.
    """

    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if raw in NON_ASSET_CODES:
        return ""
    catalog = _asset_catalog(known_assets)
    if raw in catalog:
        return catalog[raw]
    compact = _compact_asset(raw)
    if compact in catalog:
        return catalog[compact]
    base = _base_from_pair(raw)
    if base in catalog:
        return catalog[base]
    if re.fullmatch(r"[A-Z0-9]{2,20}/[A-Z0-9]{2,20}", raw):
        return raw
    if re.fullmatch(r"[A-Z0-9]{2,20}", base):
        return base
    return re.sub(r"\s+", " ", raw)


def normalize_assets(
    values: Iterable[str] | None,
    known_assets: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Normalize and de-duplicate asset labels while preserving source order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        asset = normalize_asset(str(value), known_assets).strip().upper()
        if not asset or asset in seen:
            continue
        seen.add(asset)
        normalized.append(asset)
    return tuple(normalized)


def merge_assets(
    detected: Iterable[str] | None,
    reported: Iterable[str] | None,
    known_assets: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Merge deterministic extraction with the LLM result.

    Rule extraction is first so a model omission cannot erase a detectable
    label; model-reported assets are then appended to retain long-tail assets.
    """

    return normalize_assets((*tuple(detected or ()), *tuple(reported or ())), known_assets)


#: ``document_summaries.event_type`` 是 VARCHAR(64)。事件类型是**开放集合**
#: （REGULATORY_APPROVAL / PRODUCT_LAUNCH / … 由来源与模型共同决定），不像
#: direction 那样有闭分类可收敛，所以这里只加长度上界。
EVENT_TYPE_MAX_LENGTH = 64


def normalize_event_type(value: str | None) -> str:
    """上线成形的事件类型标签。

    长度上界是**必需**的：``event_type`` 同样直接来自模型，和 ``direction``
    是同一类风险，只是这次侥幸没触发（线上最长 46 字符，列宽 64）。超出就
    截断并告警 —— 不抛异常，那会为了一个标签丢掉整篇文档的检索价值。
    """

    raw = str(value or "").strip().upper()
    if raw in UNKNOWN_VALUES:
        return "MARKET"
    token = re.sub(r"[\s-]+", "_", raw)
    if len(token) <= EVENT_TYPE_MAX_LENGTH:
        return token
    clipped = token[:EVENT_TYPE_MAX_LENGTH]
    # 尽量切在词边界上，别把最后一个词切一半
    if "_" in clipped:
        clipped = clipped.rsplit("_", 1)[0] or clipped
    logger.warning(
        "event_type longer than %d chars, truncated: %r -> %r",
        EVENT_TYPE_MAX_LENGTH,
        token,
        clipped,
    )
    return clipped


def normalize_impact_horizon(value: str | None) -> str:
    """Normalize model/source labels to the bounded retrieval taxonomy.

    Unknown free-form labels are intentionally collapsed to ``UNKNOWN``.  The
    value is persisted in both PostgreSQL and Milvus, so retaining arbitrary
    model output can bypass the shared taxonomy and overflow the vector-store
    VARCHAR field during historical reindexing.
    """

    raw = str(value or "").strip().upper()
    if raw in UNKNOWN_VALUES:
        return "UNKNOWN"
    token = re.sub(r"[\s-]+", "_", raw)
    if token in {
        "SHORT",
        "SHORT_TERM",
        "INTRADAY",
        "IMMEDIATE",
        "NOW",
        "MINUTE",
        "MINUTES",
        "MINS",
        "HOURS",
        "HOUR",
        "DAY",
        "DAYS",
    }:
        return "SHORT"
    if token in {"MEDIUM", "MEDIUM_TERM", "WEEK", "WEEKS", "DAYS_TO_WEEKS"}:
        return "MEDIUM"
    if token in {
        "LONG",
        "LONG_TERM",
        "MONTH",
        "MONTHS",
        "QUARTER",
        "QUARTERS",
        "SEVERAL_MONTHS",
        "YEAR",
        "YEARS",
    }:
        return "LONG"
    if token in {"SHORT_MEDIUM", "SHORT_TO_MEDIUM", "SHORT_AND_MEDIUM"}:
        return "SHORT_MEDIUM"
    return "UNKNOWN"


#: Direction vocabulary. The model emits single words and slash-joined pairs
#: (``bullish/upward``, ``neutral/uncertain``), so matching splits on the
#: separators and classifies by component rather than by whole string.
_BULLISH_WORDS = frozenset(
    {"BULLISH", "BULL", "UP", "UPWARD", "UPWARDS", "UPSIDE", "POSITIVE", "LONG", "GAIN", "GAINS"}
)
_BEARISH_WORDS = frozenset(
    {"BEARISH", "BEAR", "DOWN", "DOWNWARD", "DOWNSIDE", "NEGATIVE", "SHORT", "LOSS", "LOSSES"}
)
_NEUTRAL_WORDS = frozenset(
    {"NEUTRAL", "UNCERTAIN", "UNCLEAR", "UNKNOWN", "MIXED", "SIDEWAYS", "FLAT", "RANGE", "RANGING"}
)


def normalize_direction(value: str | None) -> str:
    """Normalize model/source direction labels to the bounded retrieval taxonomy.

    **Why the taxonomy has to be closed**: ``document_summaries.direction`` is
    ``VARCHAR(16)`` and the model returns free-form text — in production it
    answered ``neutral/uncertain`` (17 characters) and the resulting
    ``StringDataRightTruncation`` took down a whole document batch, because the
    pipeline commits once per batch and flushes every pending row together.
    Same failure mode, same remedy as :func:`normalize_impact_horizon`:
    retaining arbitrary model output both overflows the column and bypasses the
    shared taxonomy.

    The column had been running right at the edge: the corpus already held
    ``neutral/unknown`` (exactly 16) and ``bullish/upward`` (15). Matching whole
    strings would have collapsed those to ``UNKNOWN`` and thrown away real
    signal, so components are matched instead.

    Conflicting labels (``bullish/bearish``) become ``NEUTRAL`` rather than
    picking a side. Unrecognised labels become ``UNKNOWN``. The result is always
    one of four 7-character tokens, so no input can overflow the column.
    """

    raw = str(value or "").strip().upper()
    if raw in UNKNOWN_VALUES:
        return "UNKNOWN"
    parts = [part for part in re.split(r"[\s\-/,;_]+", raw) if part]
    bullish = any(part in _BULLISH_WORDS for part in parts)
    bearish = any(part in _BEARISH_WORDS for part in parts)
    if bullish and bearish:
        return "NEUTRAL"
    if bullish:
        return "BULLISH"
    if bearish:
        return "BEARISH"
    if any(part in _NEUTRAL_WORDS for part in parts):
        return "NEUTRAL"
    return "UNKNOWN"


def classify_asset_scope(assets: Iterable[str] | None, source: str, event_type: str) -> str:
    """Classify rows for retrieval fallback and offline distribution analysis."""

    if tuple(assets or ()):
        return "ASSET_SPECIFIC"
    if source.strip().lower() in {"federal-reserve", "fred", "macro"}:
        return "MACRO"
    if not source.strip() or source.strip().lower() == "unknown":
        return "UNKNOWN"
    if normalize_event_type(event_type) in MACRO_EVENT_TYPES:
        return "MACRO"
    return "MARKET_WIDE"


def detect_assets(text: str, known_assets: Iterable[str] | None = None) -> tuple[str, ...]:
    value = text or ""
    detected = [asset for asset, pattern in ASSET_PATTERNS.items() if re.search(pattern, value)]
    known = _asset_catalog(known_assets)
    for configured in known_assets or ():
        base = _base_from_pair(str(configured))
        if not base:
            continue
        pattern = _asset_pattern((base,))
        if re.search(pattern, value) and base not in detected:
            detected.append(known.get(_compact_asset(base), base))
    return tuple(detected)


def detect_event_type(text: str) -> str:
    for event_type, pattern in EVENT_PATTERNS:
        if re.search(pattern, text):
            return event_type
    return "MARKET"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocumentRepository:
    """Idempotent document persistence, usable with a SQLAlchemy session or in memory."""

    def __init__(self, db: Session | None = None, known_assets: Iterable[str] | None = None) -> None:
        self.db = db
        self.known_assets = tuple(known_assets or ())
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
                assets=detect_assets(f"{title}\n{content}", self.known_assets),
                event_type=detect_event_type(f"{title}\n{content}"),
                published_at=published_at,
            )
            self._records[record.document_id] = record
            return record

        existing_record = self.db.scalar(
            select(SourceDocument).where(
                or_(SourceDocument.canonical_url == canonical_url, SourceDocument.content_hash == digest)
            )
        )
        if existing_record:
            return existing_record
        db_record = SourceDocument(
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
        self.db.add(db_record)
        self.db.flush()
        return db_record


def prepare_document(
    document: DocumentInput,
    document_id: str | None = None,
    known_assets: Iterable[str] | None = None,
) -> ProcessedDocument:
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
        assets=detect_assets(tagged_text, known_assets),
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
